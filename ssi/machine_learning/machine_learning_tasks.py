from typing import List, Dict, Any, Callable, Tuple
from abc import ABC, abstractmethod
from sklearn.pipeline import Pipeline
from .adversarial import evaluate_adversarial_pipeline, create_combined_and_filtered_dataframe
from .train_model import train_and_evaluate_model, train_model, evaluate_model, evaluate
from ..feature_extraction.feature_extraction import FeatureExtractorType
from ..preprocessing.files import get_store_name_from_combined_filename, get_combined_revenue_files_in_folder
from ..files import get_features_files_in_directory, batched_writer
from .utils import store_combinations
import pandas as pd
import luigi
import joblib
import os
import json
import tqdm

# TODO add an evaluation that trains a model on one supermarket and evaluates it on another.
# Check TFIDF and CountVectorizer for the feature extraction; they use a word dictionary,
# this dictionary may be supermarket specific! i.e. features from one supermarket may not be usable with another.
# TODO Return feature extraction pipeline instead?

# TODO duplicated


class ParquetFile(luigi.ExternalTask):
    filename = luigi.Parameter()

    def output(self):
        return luigi.LocalTarget(self.filename, format=luigi.format.Nop)


class ModelEvaluator(ABC):
    @abstractmethod
    def evaluate_training(self, training_data_loader: Callable[[], pd.DataFrame]) -> Dict[str, Any]:
        pass

    @abstractmethod
    def evaluate(self, predictions_data_loader: Callable[[], pd.DataFrame]) -> Dict[str, Any]:
        pass


class ModelTrainer:
    def __init__(self,
                 model_evaluator: ModelEvaluator,
                 batch_predict_size: int = 1000,
                 parquet_engine: str = "pyarrow"):
        self._train_evaluation_dict = {}
        self._evaluation_dict = {}
        self._pipeline = None
        self._model_evaluator = model_evaluator
        self._batch_predict_size = batch_predict_size
        self._parquet_engine = parquet_engine

    @property
    def pipeline(self):
        return self._pipeline

    @property
    def model_evaluator(self) -> ModelEvaluator:
        return self._model_evaluator

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

    def fit(self,
            training_data_loader: Callable[[], pd.DataFrame],
            training_function: Callable[[pd.DataFrame, str, str, str], Any],
            training_predictions_file: str,
            **training_kwargs
            ):
        training_data = training_data_loader()
        self._pipeline, self._train_evaluation_dict = training_function(
            training_data, training_kwargs)
        self.batch_predict(training_data_loader,
                           training_predictions_file,
                           lambda dataframe: self.model_evaluator.evaluate_training(dataframe))

    def predict(self,
                predictions_data_loader: Callable[[], pd.DataFrame],
                predictions_file: str):
        self.batch_predict(predictions_data_loader,
                           predictions_file,
                           lambda dataframe: self.model_evaluator.evaluate(dataframe))

    def batch_predict(self,
                      predictions_data_loader: Callable[[], pd.DataFrame],
                      predictions_file: str,
                      batch_size: int,
                      evaluation_function: Callable[[pd.DataFrame], Dict[str, Any]],
                      ):
        dataframe = predictions_data_loader()
        batched_writer(predictions_file,
                       dataframe,
                       batch_size,
                       lambda batch: self.__predict(batch),
                       pipeline=self.pipeline,
                       feature_column=self.features_column)

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
        for prediction_index, prediction in enumerate(probabilities):
            batch_dataframe[f"{probability_column_prefix}_{prediction_index}"] = prediction[prediction_index]

        batch_dataframe[prediction_column] = pipeline.predict(
            X.values.tolist())

    def write_model(self, model_file):
        joblib.dump(self.pipeline, model_file)

    def write_training_evaluation(self, evaluation_file):
        json.dump(self.train_evaluation_dict, evaluation_file)

    def write_evaluation(self, evaluation_file):
        json.dump(self.evaluation_dict, evaluation_file)


class TrainAdversarialModelTask(luigi.Task, ModelEvaluator):
    """
    Train an adversarial model to predict the store id based on the receipt text column.
    If we can predict the store id based on the receipt text, then the receipt text between
    stores are very different.

    """
    store1_filename = luigi.PathParameter()
    store2_filename = luigi.PathParameter()
    output_directory = luigi.PathParameter()
    feature_extractor = luigi.EnumParameter(enum=FeatureExtractorType)
    model_type = luigi.Parameter()

    store_id_column = luigi.Parameter()
    receipt_text_column = luigi.Parameter()
    features_column = luigi.Parameter(default="features")
    test_size = luigi.FloatParameter(default=0.2)
    batch_predict_size = luigi.IntParameter(default=1000)
    parquet_engine = luigi.Parameter()
    verbose = luigi.BoolParameter(default=False)

    @property
    def model_trainer(self) -> ModelTrainer:
        return ModelTrainer(
            model_evaluator=self,
            batch_predict_size=self.batch_predict_size,
            parquet_engine=self.parquet_engine
        )

    def get_model_filename(self, store1: str, store2: str) -> str:
        return os.path.join(
            self.output_directory, f"adversarial_{store1}_{store2}_{self.feature_extractor.value}_{self.model_type}_model.joblib")

    def get_training_predictions_filename(self, store1: str, store2: str) -> str:
        return os.path.join(
            self.output_directory, f"adversarial_{store1}_{store2}_{self.feature_extractor.value}_{self.model_type}_training_predictions.parquet")

    def get_predictions_filename(self, store1: str, store2: str) -> str:
        return os.path.join(
            self.output_directory, f"adversarial_{store1}_{store2}_{self.feature_extractor.value}_{self.model_type}_predictions.parquet")

    def get_evaluation_filename(self, store1: str, store2: str) -> str:
        return os.path.join(
            self.output_directory, f"adversarial_{store1}_{store2}_{self.feature_extractor.value}_{self.model_type}.evaluation.json")

    def requires(self):
        return [ParquetFile(self.store1_filename), ParquetFile(self.store2_filename)]

    def output(self):
        store1 = get_store_name_from_combined_filename(self.store1_filename)
        store2 = get_store_name_from_combined_filename(self.store2_filename)
        return {
            "model": luigi.LocalTarget(self.get_model_filename(store1, store2), format=luigi.format.Nop),
            "training_predictions_file": luigi.LocalTarget(self.get_training_predictions_filename(store1, store2), format=luigi.format.Nop),
            "predictions_file": luigi.LocalTarget(self.get_predictions_filename(store1, store2), format=luigi.format.Nop),
            "evaluation": luigi.LocalTarget(self.get_evaluation_filename(store1, store2))
        }

    def read_parquet_data(self, store1_file: str) -> pd.DataFrame:
        store1_dataframe = pd.read_parquet(
            store1_file, engine=self.parquet_engine)

        return store1_dataframe

    def get_adversarial_data(self, store1_file, store_name: str):
        store1_dataframe = self.read_parquet_data(store1_file)
        store1_dataframe = store1_dataframe.drop_duplicates(
            [self.receipt_text_column, self.store_id_column])
        store1_dataframe[self.store_id_column] = store_name
        return store1_dataframe

    def get_all_adversarial_data(self, store1: str, store2: str, store1_file, store2_file) -> pd.DataFrame:
        store1_dataframe = self.get_adversarial_data(store1_file, store1)
        store2_dataframe = self.get_adversarial_data(store2_file, store2)
        return create_combined_and_filtered_dataframe(store1_dataframe,
                                                      store2_dataframe,
                                                      self.store_id_column,
                                                      self.receipt_text_column,
                                                      self.features_column)

    def train_adversarial_model(self,
                                adversarial_dataframe: pd.DataFrame,
                                features_column: str,
                                store_id_column: str,
                                model_type: str,
                                test_size: float = 0.2,
                                verbose: bool = False
                                ) -> Tuple[Pipeline, Dict[str, Any]]:
        return train_and_evaluate_model(adversarial_dataframe,
                                        features_column,
                                        store_id_column,
                                        model_type,
                                        test_size=test_size,
                                        evaluation_function=evaluate_adversarial_pipeline,
                                        verbose=verbose)

    def evaluate_training(self, training_data_loader: Callable[[], pd.DataFrame]) -> Dict[str, Any]:
        return dict()

    def evaluate(self, predictions_data_loader: Callable[[], pd.DataFrame]) -> Dict[str, Any]:
        return dict()

    def run(self):
        print(
            f"Running adversarial model training task for {self.store1_filename} and {self.store2_filename}")
        store1 = get_store_name_from_combined_filename(self.store1_filename)
        store2 = get_store_name_from_combined_filename(self.store2_filename)
        print(f"Store1: {store1}, Store2: {store2}")
        with self.input()[0].open("r") as store1_file, self.input()[1].open("r") as store2_file:
            print("Reading parquet files")
            adversarial_dataframe = self.get_all_adversarial_data(
                store1, store2, store1_file, store2_file)

            print("Training adversarial model & writing training predictions to disk")
            with self.output()["training_predictions_file"].open("w") as training_predictions_file:
                self.model_trainer.fit(lambda: adversarial_dataframe,
                                       self.train_adversarial_model,
                                       training_predictions_file,
                                       features_column=self.features_column,
                                       store_id_column=self.store_id_column,
                                       model_type=self.model_type,
                                       test_size=self.test_size,
                                       verbose=self.verbose)

            print("Writing raw predictions to disk")
            with self.output()["predictions_file"].open("w") as predictions_file:
                self.model_trainer.predict(lambda: adversarial_dataframe,
                                           predictions_file)

            print("Writing adversarial model to disk")
            with self.output()["model"].open("w") as model_file:
                self.model_trainer.write_model(model_file)

            print("Writing evaluation to disk")
            with self.output()["evaluation"].open("w") as evaluation_file:
                self.model_trainer.write_evaluation(evaluation_file)


class TrainAllAdversarialModels(luigi.WrapperTask):
    input_directory = luigi.PathParameter()
    output_directory = luigi.PathParameter()
    feature_extractor = luigi.EnumParameter(enum=FeatureExtractorType)
    model_type = luigi.Parameter()
    store_id_column = luigi.Parameter()
    receipt_text_column = luigi.Parameter()
    features_column = luigi.Parameter(default="features")
    test_size = luigi.FloatParameter(default=0.2)
    parquet_engine = luigi.Parameter()
    verbose = luigi.BoolParameter(default=False)

    filename_prefix = luigi.Parameter()

    def requires(self):
        store_filenames = [os.path.join(self.input_directory, filename)
                           for filename in get_features_files_in_directory(
                               self.input_directory, self.filename_prefix)
                           if f"{self.feature_extractor.value}.parquet" in filename]

        return [TrainAdversarialModelTask(store1_filename=store1_filename,
                                          store2_filename=store2_filename,
                                          output_directory=self.output_directory,
                                          feature_extractor=self.feature_extractor,
                                          model_type=self.model_type,
                                          store_id_column=self.store_id_column,
                                          receipt_text_column=self.receipt_text_column,
                                          features_column=self.features_column,
                                          test_size=self.test_size,
                                          parquet_engine=self.parquet_engine,
                                          verbose=self.verbose)
                for store1_filename, store2_filename in store_combinations(store_filenames)]


class CrossStoreEvaluation(luigi.Task):
    store1_filename = luigi.PathParameter()
    store2_filename = luigi.PathParameter()
    output_directory = luigi.PathParameter()
    feature_extractor = luigi.EnumParameter(enum=FeatureExtractorType)
    model_type = luigi.Parameter()

    label_column = luigi.Parameter()
    receipt_text_column = luigi.Parameter()
    features_column = luigi.Parameter(default="features")
    test_size = luigi.FloatParameter(default=0.2)
    parquet_engine = luigi.Parameter()
    verbose = luigi.BoolParameter(default=False)

    def requires(self):
        return [(ParquetFile(combinations[0]), ParquetFile(combinations[1]))
                for combinations in [
                    (self.store1_filename, self.store2_filename),
                    (self.store2_filename, self.store1_filename)
        ]
        ]

    def get_model_filename(self, store1: str, store2: str) -> str:
        return os.path.join(
            self.output_directory, f"cross_store_{store1}_{store2}_{self.feature_extractor.value}_{self.model_type}_{self.label_column}_model.joblib")

    def get_train_evaluation_filename(self, store1: str, store2: str) -> str:
        return os.path.join(
            self.output_directory, f"cross_store_{store1}_{store2}_{self.feature_extractor.value}_{self.model_type}_{self.label_column}_train_evaluation.json")

    def get_evaluation_filename(self, store1: str, store2: str) -> str:
        return os.path.join(
            self.output_directory, f"cross_store_{store1}_{store2}_{self.feature_extractor.value}_{self.model_type}_{self.label_column}_evaluation.json")

    def get_evaluation_filename(self, store1: str, store2: str) -> str:
        return os.path.join(
            self.output_directory, f"cross_store_{store1}_{store2}_{self.feature_extractor.value}_{self.model_type}_{self.label_column}_evaluation.json")

    def output(self):
        store1 = get_store_name_from_combined_filename(self.store1_filename)
        store2 = get_store_name_from_combined_filename(self.store2_filename)
        return [
            {
                f"model_{combination_store1}_{combination_store2}": luigi.LocalTarget(self.get_model_filename(combination_store1, combination_store2), format=luigi.format.Nop),
                f"train_evaluation_{combination_store1}_{combination_store2}": luigi.LocalTarget(self.get_evaluation_filename(combination_store1, combination_store2)),
                # TODO add evaluation filename here.
                f"evaluation_{combination_store1}_{combination_store2}": luigi.LocalTarget(self.get_evaluation_filename(combination_store1, combination_store2))
            }
            for combination_store1, combination_store2 in [(store1, store2), (store2, store1)
                                                           ]
        ]

    def get_store_data(self, store_file) -> pd.DataFrame:
        store_dataframe = pd.read_parquet(
            store_file, engine=self.parquet_engine)
        store_dataframe = store_dataframe.drop_duplicates(
            [self.receipt_text_column, self.label_column])

        return store_dataframe

    def get_all_store_data(self, store1_file, store2_file) -> Tuple[pd.DataFrame, pd.DataFrame]:
        store1_dataframe = self.get_store_data(store1_file)
        store2_dataframe = self.get_store_data(store2_file)
        return store1_dataframe, store2_dataframe

    def run(self):

        print(
            f"Running cross store evaluation training task for {self.store1_filename} and {self.store2_filename}")
        for index, input_combinations in enumerate(self.input()):
            store1 = get_store_name_from_combined_filename(
                input_combinations[0].path)
            store2 = get_store_name_from_combined_filename(
                input_combinations[1].path)
            print(f"Train on: {store1}, Evaluate on: {store2}")

            with input_combinations[0].open("r") as store1_file, input_combinations[1].open("r") as store2_file:
                print("Reading parquet files")
                store1_dataframe, store2_dataframe = self.get_all_store_data(
                    store1_file, store2_file)
                print(f"Training model on {store1}")
                pipeline, train_evaluation_dict = train_and_evaluate_model(store1_dataframe,
                                                                           self.features_column,
                                                                           self.label_column,
                                                                           self.model_type,
                                                                           test_size=self.test_size,
                                                                           verbose=self.verbose)
                print(f"Evaluating model on {store2}")
                evaluation_dict = evaluate_model(pipeline,
                                                 store2_dataframe,
                                                 self.features_column,
                                                 self.label_column,
                                                 evaluation_function=evaluate,
                                                 )

                print("Writing model to disk")
                outputs = self.output()[index]
                with outputs[f"model_{store1}_{store2}"].open("w") as model_file:
                    joblib.dump(pipeline, model_file)

                print("Writing training evaluation to disk")
                with outputs[f"train_evaluation_{store1}_{store2}"].open("w") as train_evaluation_file:
                    json.dump(train_evaluation_dict, train_evaluation_file)

                print("Writing evaluation to disk")
                with outputs[f"evaluation_{store1}_{store2}"].open("w") as evaluation_file:
                    json.dump(evaluation_dict, evaluation_file)


class AllCrossStoreEvaluations(luigi.WrapperTask):
    input_directory = luigi.PathParameter()
    output_directory = luigi.PathParameter()
    feature_extractor = luigi.EnumParameter(enum=FeatureExtractorType)
    model_type = luigi.Parameter()
    receipt_text_column = luigi.Parameter()
    features_column = luigi.Parameter(default="features")
    label_column = luigi.Parameter()
    test_size = luigi.FloatParameter(default=0.2)
    parquet_engine = luigi.Parameter()
    verbose = luigi.BoolParameter(default=False)

    filename_prefix = luigi.Parameter()

    def requires(self):
        store_filenames = [os.path.join(self.input_directory, filename)
                           for filename in get_features_files_in_directory(
                               self.input_directory, self.filename_prefix)
                           if self.feature_extractor.value in filename]

        return [CrossStoreEvaluation(store1_filename=store1_filename,
                                     store2_filename=store2_filename,
                                     output_directory=self.output_directory,
                                     feature_extractor=self.feature_extractor,
                                     model_type=self.model_type,
                                     receipt_text_column=self.receipt_text_column,
                                     features_column=self.features_column,
                                     label_column=self.label_column,
                                     test_size=self.test_size,
                                     parquet_engine=self.parquet_engine,
                                     verbose=self.verbose)
                for store1_filename, store2_filename in store_combinations(store_filenames)]


class TrainModelOnPeriod(luigi.Task):
    input_filename = luigi.PathParameter()
    output_directory = luigi.PathParameter()
    feature_extractor = luigi.EnumParameter(enum=FeatureExtractorType)
    model_type = luigi.Parameter()

    label_column = luigi.Parameter()
    receipt_text_column = luigi.Parameter()
    features_column = luigi.Parameter(default="features")
    batch_size = luigi.IntParameter(default=1000)
    parquet_engine = luigi.Parameter()
    verbose = luigi.BoolParameter(default=False)
    period_column = luigi.Parameter()
    train_period = luigi.Parameter()

    @property
    def train_from_scratch(self) -> List[FeatureExtractorType]:
        """ Return the feature extractors that require training from scratch.
        TFIDF and CountVectorizer require a word dictionary that is specific to the
        receipt texts seen at training time. To evaluate these models correctly we cannot
        use the files with extracted features as they are trained on the full dataset, not
        the specific period that we may want to evaluate.
        """
        return {
            FeatureExtractorType.tfidf_char,
            FeatureExtractorType.tfidf_word,
            FeatureExtractorType.count_char,
            FeatureExtractorType.count_vectorizer
        }

    def requires(self):
        return ParquetFile(self.input_filename)

    def get_model_filename(self) -> str:
        return os.path.join(self.output_directory, f"{self.feature_extractor.value}_{self.model_type}_{self.label_column}_{self.train_period}.joblib")

    def get_predictions_filename(self) -> str:
        return os.path.join(self.output_directory, f"{self.feature_extractor.value}_{self.model_type}_{self.label_column}_{self.train_period}.predictions.parquet")

    def get_evaluations_filename(self) -> str:
        return os.path.join(self.output_directory, f"{self.feature_extractor.value}_{self.model_type}_{self.label_column}_{self.train_period}.evaluation.json")

    def output(self):
        return {
            "model": luigi.LocalTarget(self.get_model_filename(), format=luigi.format.Nop),
            "model_predictions": luigi.LocalTarget(self.get_predictions_filename(), format=luigi.format.Nop),
            "evaluation": luigi.LocalTarget(self.get_evaluations_filename())
        }

    def get_data_for_period(self, input_file):
        dataframe = pd.read_parquet(input_file, engine=self.parquet_engine)
        dataframe = dataframe.drop_duplicates(
            [self.receipt_text_column, self.label_column])
        dataframe["is_train"] = dataframe[self.period_column] == self.train_period
        return dataframe

    def predict_batch(self,
                      batch_dataframe: pd.DataFrame,
                      progress_bar: tqdm.tqdm,
                      pipeline,
                      features_column: str) -> pd.DataFrame:
        X = batch_dataframe[features_column]

        progress_bar.set_description("Predicting probabilities")
        probabilities = pipeline.predict_proba(X.values.tolist())
        for probability_vector in enumerate(probabilities):
            for class_label, probability_value in zip(pipeline.classes_, probability_vector):
                batch_dataframe[f"y_proba_{class_label}"] = probability_value

        batch_dataframe["y_pred"] = pipeline.predict(X.values.tolist())

    def run(self):
        print(
            f"Training model: {self.model_type} on period: {self.train_period}")
        with self.input().open() as input_file:
            dataframe = self.get_data_for_period(input_file)

            train_dataframe = dataframe[dataframe["is_train"] == True]
            if self.feature_extractor in self.train_from_scratch:
                raise NotImplementedError(
                    "Training feature extractor from scratch not implemented")

            pipeline = train_model(train_dataframe,
                                   model_type=self.model_type,
                                   feature_column=self.features_column,
                                   label_column=self.label_column,
                                   verbose=self.verbose)

            print("Writing model to disk")
            with self.output()["model"].open("w") as model_file:
                joblib.dump(pipeline, model_file)

            # Predict labels on dataframe in batches
            print("Writing predictions to disk")
            with self.output()["model_predictions"].open("w") as predictions_file:
                batched_writer(predictions_file,
                               dataframe,
                               self.batch_size,
                               self.predict_batch,
                               pipeline=pipeline,
                               features_column=self.features_column)

            print("Evaluating model")
            evaluation_dict = evaluate_model(pipeline,
                                             dataframe,
                                             self.features_column,
                                             self.label_column,
                                             evaluation_function=evaluate,
                                             )
            with self.output()["evaluation"].open("w") as evaluation_file:
                json.dump(evaluation_dict, evaluation_file)


class TrainModelOnAllPeriods(luigi.WrapperTask):
    input_directory = luigi.PathParameter()
    output_directory = luigi.PathParameter()
    feature_extractor = luigi.EnumParameter(enum=FeatureExtractorType)
    model_type = luigi.Parameter()

    filename_prefix = luigi.Parameter()
    label_column = luigi.Parameter()
    receipt_text_column = luigi.Parameter()
    features_column = luigi.Parameter(default="features")
    batch_size = luigi.IntParameter(default=1000)
    parquet_engine = luigi.Parameter()
    verbose = luigi.BoolParameter(default=False)
    period_columns = luigi.ListParameter()

    def requires(self):
        return [TrainModelOnPeriod(input_filename=os.path.join(self.input_directory, feature_filename),
                                   output_directory=self.output_directory,
                                   feature_extractor=self.feature_extractor,
                                   model_type=self.model_type,
                                   label_column=self.label_column,
                                   receipt_text_column=self.receipt_text_column,
                                   features_column=self.features_column,
                                   batch_size=self.batch_size,
                                   parquet_engine=self.parquet_engine,
                                   verbose=self.verbose,
                                   period_column=period_column,
                                   train_period=period)
                for feature_filename in get_features_files_in_directory(self.input_directory, self.filename_prefix, self.feature_extractor.value)
                for period_column in self.period_columns
                for period in pd.read_parquet(os.path.join(self.input_directory, feature_filename), engine=self.parquet_engine)[period_column].unique()
                ]
