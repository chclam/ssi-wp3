from .parquet import convert_to_parquet
from .preprocess_data import convert_ah_receipts
from .clean import CleanCPIFile
import luigi
import os


class CsvFile(luigi.ExternalTask):
    input_filename = luigi.PathParameter()

    def output(self):
        return luigi.LocalTarget(self.input_filename)


class ExcelFile(luigi.ExternalTask):
    input_filename = luigi.PathParameter()

    def output(self):
        return luigi.LocalTarget(self.input_filename, format=luigi.format.Nop)


class ConvertAHReceipts(luigi.Task):
    """ Convert an AH receipts file in Excel format to a parquet file.

    """
    input_filename = luigi.PathParameter()
    output_filename = luigi.PathParameter()
    coicop_sheets_prefix = luigi.Parameter(default="coi")

    def requires(self):
        return ExcelFile(input_filename=self.input_filename)

    def run(self):
        with self.input().open('r') as input_file:
            with self.output().open('w') as output_file:
                ah_receipts_df = convert_ah_receipts(
                    input_file, self.coicop_sheets_prefix)
                ah_receipts_df.to_parquet(output_file, engine="pyarrow")

    def output(self):
        return luigi.LocalTarget(self.output_filename, format=luigi.format.Nop)


class ConvertCSVToParquet(luigi.Task):
    """ This task converts a CSV file to a parquet file.

    Parameters
    ----------
    input_filename : luigi.PathParameter
        The input filename of the original CSV file.

    output_filename : luigi.PathParameter
        The output filename for the parquet file.

    delimiter : luigi.Parameter
        The delimiter used in the CSV file.

    """
    input_filename = luigi.PathParameter()
    output_directory = luigi.PathParameter()
    delimiter = luigi.Parameter(default=';')
    encoding = luigi.Parameter(default='utf-8')
    extension = luigi.Parameter(default='.csv')
    decimal = luigi.Parameter(default=',')
    clean_cpi = luigi.BoolParameter(default=False)

    def get_parquet_filename(self):
        return os.path.join(self.output_directory, os.path.basename(
            self.input_filename).replace(self.extension, ".parquet"))

    def requires(self):
        if self.clean_cpi:
            return CleanCPIFile(input_filename=self.input_filename, output_filename=self.get_parquet_filename())
        return CsvFile(input_filename=self.input_filename)

    def run(self):
        with self.input().open('r') as input_file:
            with self.output().open('w') as output_file:
                convert_to_parquet(self.input_filename,
                                   input_file,
                                   output_file,
                                   delimiter=self.delimiter,
                                   encoding=self.encoding,
                                   extension=self.extension,
                                   decimal=',')

    def output(self):
        return luigi.LocalTarget(self.get_parquet_filename(), format=luigi.format.Nop)


class ConvertAllCSVToParquet(luigi.WrapperTask):
    input_directory = luigi.Parameter()
    output_directory = luigi.Parameter()
    delimiter = luigi.Parameter(default=';')
    encoding = luigi.Parameter(default='utf-8')
    extension = luigi.Parameter(default='.csv')
    decimal = luigi.Parameter(default=',')
    clean_cpi = luigi.BoolParameter(default=False)

    def requires(self):
        return [ConvertCSVToParquet(input_filename=os.path.join(self.input_directory, input_filename),
                                    output_directory=self.output_directory,
                                    delimiter=self.delimiter,
                                    encoding=self.encoding,
                                    extension=self.extension,
                                    decimal=self.decimal,
                                    clean_cpi=self.clean_cpi)
                for input_filename in os.listdir(self.input_directory)
                if input_filename.endswith(self.extension)
                ]
