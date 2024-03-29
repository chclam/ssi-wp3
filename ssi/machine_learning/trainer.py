from typing import Any, Callable, Dict
from sklearn.metrics import confusion_matrix
from ..files import batched_writer
from .evaluate import ModelEvaluator
import pandas as pd
import json
import joblib
import tqdm


class ModelTrainer:
    def __init__(self,
                 model_evaluator: ModelEvaluator,
                 features_column: str,
                 label_column: str,
                 prediction_column: str = "y_pred",
                 batch_predict_size: int = 1000,
                 parquet_engine: str = "pyarrow"):
        self._train_evaluation_dict = {}
        self._evaluation_dict = {}
        self._pipeline = None
        self._model_evaluator = model_evaluator
        self._features_column = features_column
        self._label_column = label_column
        self._prediction_column = prediction_column
        self._batch_predict_size = batch_predict_size
        self._parquet_engine = parquet_engine

    @property
    def pipeline(self):
        return self._pipeline

    @property
    def model_evaluator(self) -> ModelEvaluator:
        return self._model_evaluator

    @property
    def features_column(self) -> str:
        return self._features_column

    @property
    def label_column(self) -> str:
        return self._label_column

    @property
    def prediction_column(self) -> str:
        return self._prediction_column

    @property
    def batch_predict_size(self) -> int:
        return self._batch_predict_size

    @batch_predict_size.setter
    def batch_predict_size(self, value: int):
        self._batch_predict_size = value

    @property
    def parquet_engine(self) -> str:
        return self._parquet_engine

    @property
    def train_evaluation_dict(self) -> Dict[str, Any]:
        return self._train_evaluation_dict

    @train_evaluation_dict.setter
    def train_evaluation_dict(self, value: Dict[str, Any]):
        self._train_evaluation_dict = value

    @property
    def evaluation_dict(self) -> Dict[str, Any]:
        return self._evaluation_dict

    @evaluation_dict.setter
    def evaluation_dict(self, value: Dict[str, Any]):
        self._evaluation_dict = value

    def get_kwargs_with_prefix(self, prefix: str, **kwargs) -> Dict[str, Any]:
        return {key: value for key, value in kwargs.items() if key.startswith(prefix)}

    def fit(self,
            training_data: pd.DataFrame,
            training_function: Callable[[pd.DataFrame, str, str, str], Any],
            training_predictions_file: str,
            **training_kwargs
            ):
        self._pipeline = training_function(
            training_data, **training_kwargs)
        self.train_evaluation_dict = self.batch_predict(training_data,
                                                        training_predictions_file,
                                                        lambda dataframe: self.model_evaluator.evaluate_training(
                                                            dataframe)
                                                        )

    def predict(self,
                predictions_data: pd.DataFrame,
                predictions_file: str
                ):
        self.batch_predict(predictions_data,
                           predictions_file,
                           lambda dataframe: self.model_evaluator.evaluate(
                               dataframe)
                           )

    def batch_statistics(self, dataframe: pd.DataFrame, label_column: str, predicted_label_column: str) -> pd.DataFrame:
        y_true = dataframe[label_column]
        y_pred = dataframe[predicted_label_column]
        return pd.DataFrame(confusion_matrix(y_true, y_pred))

    def batch_predict(self,
                      predictions_data: pd.DataFrame,
                      predictions_file: str,
                      batch_size: int,
                      evaluation_function: Callable[[
                          pd.DataFrame], Dict[str, Any]]
                      ) -> Dict[str, Any]:
        batch_statistics = batched_writer(predictions_file,
                                          predictions_data,
                                          batch_size,
                                          process_batch=lambda batch: self.__predict(
                                              batch),
                                          batch_statistics=lambda dataframe: self.batch_statistics(
                                              dataframe=dataframe,
                                              label_column=self.label_column,
                                              predicted_label_column=self._prediction_column),
                                          pipeline=self.pipeline,
                                          feature_column=self.features_column,
                                          prediction_column=self.prediction_column)
        return evaluation_function(batch_statistics)

    def __predict(self,
                  batch_dataframe: pd.DataFrame,
                  progress_bar: tqdm.tqdm,
                  pipeline,
                  features_column: str,
                  probability_column_prefix: str = "y_proba",
                  prediction_column: str = "y_pred") -> pd.DataFrame:
        X = batch_dataframe[features_column]

        progress_bar.set_description("Predicting probabilities")
        probabilities = pipeline.predict_proba(X.values.tolist())
        for index, probability_vector in enumerate(probabilities):
            for class_label, probability_value in zip(pipeline.classes_, probability_vector):
                batch_dataframe.iloc[index][f"{probability_column_prefix}_{class_label}"] = probability_value

        batch_dataframe[prediction_column] = pipeline.predict(
            X.values.tolist())
        return batch_dataframe

    def write_model(self, model_file):
        joblib.dump(self.pipeline, model_file)

    def write_training_evaluation(self, evaluation_file):
        json.dump(self.train_evaluation_dict, evaluation_file)

    def write_evaluation(self, evaluation_file):
        json.dump(self.evaluation_dict, evaluation_file)
