from abc import ABC
import subprocess
import sys
import pandas as pd
import numpy as np
import torch
from sktime.performance_metrics.forecasting import mean_absolute_percentage_error
from typing import Iterator
from pyspark.sql.functions import collect_list, pandas_udf
from pyspark.sql import DataFrame
from mmf_sa.models.abstract_model import ForecastingRegressor


class MomentForecaster(ForecastingRegressor):
    def __init__(self, params):
        super().__init__(params)
        self.params = params
        self.device = None
        self.model = None
        self.install("git+https://github.com/moment-timeseries-foundation-model/moment.git")

    def install(self, package: str):
        subprocess.check_call([sys.executable, "-m", "pip", "install", package, "--quiet"])

    def create_horizon_timestamps_udf(self):
        @pandas_udf('array<timestamp>')
        def horizon_timestamps_udf(batch_iterator: Iterator[pd.Series]) -> Iterator[pd.Series]:
            batch_horizon_timestamps = []
            for batch in batch_iterator:
                for series in batch:
                    last = series.max()
                    horizon_timestamps = []
                    for i in range(self.params["prediction_length"]):
                        last = last + self.one_ts_offset
                        horizon_timestamps.append(last)
                    batch_horizon_timestamps.append(np.array(horizon_timestamps))
            yield pd.Series(batch_horizon_timestamps)
        return horizon_timestamps_udf

    def prepare_data(self, df: pd.DataFrame, future: bool = False, spark=None) -> DataFrame:
        df = spark.createDataFrame(df)
        df = (
            df.groupBy(self.params.group_id)
            .agg(
                collect_list(self.params.date_col).alias('ds'),
                collect_list(self.params.target).alias('y'),
            ))
        return df

    def predict(self,
                hist_df: pd.DataFrame,
                val_df: pd.DataFrame = None,
                curr_date=None,
                spark=None):
        hist_df = self.prepare_data(hist_df, spark=spark)
        forecast_udf = self.create_predict_udf()
        horizon_timestamps_udf = self.create_horizon_timestamps_udf()
        # Todo figure out the distribution strategy
        forecast_df = (
            hist_df.repartition(4)
            .select(
                hist_df.unique_id,
                horizon_timestamps_udf(hist_df.ds).alias("ds"),
                forecast_udf(hist_df.y).alias("y"))
        ).toPandas()

        forecast_df = forecast_df.reset_index(drop=False).rename(
            columns={
                "unique_id": self.params.group_id,
                "ds": self.params.date_col,
                "y": self.params.target,
            }
        )

        # Todo
        #forecast_df[self.params.target] = forecast_df[self.params.target].clip(0.01)

        return forecast_df, self.model

    def forecast(self, df: pd.DataFrame, spark=None):
        return self.predict(df, spark=spark)

    def calculate_metrics(
        self, hist_df: pd.DataFrame, val_df: pd.DataFrame, curr_date, spark=None
    ) -> list:
        pred_df, model_pretrained = self.predict(hist_df, val_df, curr_date, spark)
        keys = pred_df[self.params["group_id"]].unique()
        metrics = []
        if self.params["metric"] == "smape":
            metric_name = "smape"
        else:
            raise Exception(f"Metric {self.params['metric']} not supported!")
        for key in keys:
            actual = val_df[val_df[self.params["group_id"]] == key][self.params["target"]].to_numpy()
            forecast = pred_df[pred_df[self.params["group_id"]] == key][self.params["target"]].to_numpy()[0]
            try:
                if metric_name == "smape":
                    metric_value = mean_absolute_percentage_error(actual, forecast, symmetric=True)
                metrics.extend(
                    [(
                        key,
                        curr_date,
                        metric_name,
                        metric_value,
                        actual,
                        forecast,
                        b'',
                    )])
            except:
                pass
        return metrics

    def create_predict_udf(self):
        @pandas_udf('array<double>')
        def predict_udf(batch_iterator: Iterator[pd.Series]) -> Iterator[pd.Series]:
            import torch
            import pandas as pd
            for batch in batch_iterator:
                batch_forecast = []
                for series in batch:
                    # takes in tensor of shape [batchsize, n_channels, context_length]
                    context = list(series)
                    if len(context) < 512:
                        input_mask = [1] * len(context) + [0] * (512 - len(context))
                        context = context + [0] * (512 - len(context))
                    else:
                        input_mask = [1] * 512
                        context = context[-512:]
                    input_mask = torch.reshape(torch.tensor(input_mask), (1, 512))
                    context = torch.reshape(torch.tensor(context), (1, 1, 512)).to(dtype=torch.float32)
                    output = self.model(context, input_mask=input_mask)
                    forecast = output.forecast.squeeze().tolist()
                    batch_forecast.append(forecast)
            yield pd.Series(batch_forecast)
        return predict_udf


class Moment1Large(MomentForecaster):
    def __init__(self, params):
        super().__init__(params)
        from momentfm import MOMENTPipeline
        self.params = params
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = MOMENTPipeline.from_pretrained(
            "AutonLab/MOMENT-1-large",
            model_kwargs={
                'task_name': 'forecasting',
                'forecast_horizon': self.params["prediction_length"],
                'head_dropout': 0.1,
                'weight_decay': 0,
                'freeze_encoder': True,  # Freeze the patch embedding layer
                'freeze_embedder': True,  # Freeze the transformer encoder
                'freeze_head': False,  # The linear forecasting head must be trained
            },
        )
        self.model.init()

