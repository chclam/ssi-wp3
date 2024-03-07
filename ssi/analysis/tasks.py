from typing import Dict, Callable
from .files import get_combined_revenue_files_in_directory
from .products import *
import pandas as pd
import pathlib
import luigi
import os


class StoreFile(luigi.ExternalTask):
    input_filename = luigi.PathParameter()

    def output(self):
        return luigi.LocalTarget(self.input_filename)


class StoreProductAnalysis(luigi.Task):
    """ This task analyzes the store product inventory and dynamics.

    """
    input_filename = luigi.PathParameter()
    output_directory = luigi.PathParameter()
    parquet_engine = luigi.Parameter(default="pyarrow")

    store_name = luigi.Parameter()
    period_column = luigi.Parameter()
    receipt_text_column = luigi.Parameter()
    product_id_column = luigi.Parameter()
    coicop_column = luigi.Parameter()

    @property
    def product_analysis_functions(self) -> Dict[str, Callable]:
        value_columns = [self.period_column, self.receipt_text_column]
        return {
            "unique_column_values": lambda dataframe: unique_column_values(dataframe, value_columns),
            "unique_coicop_values_per_coicop": lambda dataframe: unique_column_values_per_coicop(dataframe, self.coicop_column, value_columns),
            "unique_column_values_per_period": lambda dataframe: unique_column_values_per_period(dataframe, self.period_column, value_columns),
            "texts_per_ean_histogram": lambda dataframe: texts_per_ean_histogram(dataframe, self.receipt_text_column, self.product_id_column),
            "log_texts_per_ean_histogram": lambda dataframe: log_texts_per_ean_histogram(dataframe, self.receipt_text_column, self.product_id_column),
            "compare_products_per_period": lambda dataframe: compare_products_per_period(dataframe, self.period_column, value_columns),
        }

    def requires(self):
        return StoreFile(self.input_filename)

    def output(self):
        products_directory = os.path.join(self.output_directory, "products")
        return [luigi.LocalTarget(os.path.join(products_directory, f"{self.store_name}_analysis_{function_name}.parquet"), format=luigi.format.Nop)
                for function_name in self.product_analysis_functions.keys()]

    def run(self):
        dataframe = pd.read_parquet(
            self.input(), engine=self.parquet_engine)
        for function_name, function in self.product_analysis_functions.items():
            result_df = function(dataframe)
            result_df.to_parquet(
                self.output()[function_name], engine=self.parquet_engine)

    # def output(self):
    #    return luigi.LocalTarget(os.path.join(self.output_directory, "product_analysis_results.csv"), format=luigi.format.Nop)


class AllStoresAnalysis(luigi.WrapperTask):
    """ This task analyses the product inventory and dynamics for all stores in a certain
    directory.

    Parameters
    ----------

    input_directory : luigi.PathParameter
        The directory containing the store product inventory files.

    output_directory : luigi.PathParameter
        The directory to store the analysis results.

    project_prefix : luigi.Parameter
        The project prefix used in the revenue file names.
    """
    input_directory = luigi.PathParameter()
    output_directory = luigi.PathParameter()
    project_prefix = luigi.Parameter(default="ssi")

    def requires(self):
        return [StoreProductAnalysis(filename, self.output_directory)
                for filename in get_combined_revenue_files_in_directory(self.input_directory, project_prefix=self.project_prefix)]
