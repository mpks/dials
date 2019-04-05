"""
Error model classes for scaling.
"""
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
import logging
from math import log, exp
from dials.array_family import flex
from scitbx import sparse
from libtbx.table_utils import simple_table
from dials.util import Sorry

logger = logging.getLogger("dials.scale")


def get_error_model(error_model_type):
    """Return the correct error model class from a params option."""
    if error_model_type == "basic":
        return BasicErrorModel
    else:
        raise Sorry("Invalid choice of error model.")


class BasicErrorModel(object):
    """
    Object to manage calculation of deviations for an error model.
    """

    def __init__(self, Ih_table, n_bins=10):
        logger.info("Initialising an error model for refinement.")
        self.Ih_table = Ih_table
        self.n_bins = n_bins
        self.binning_info = {}
        # First select on initial delta
        self.filter_large_deviants(cutoff=6.0)
        self.n_h = self.Ih_table.calc_nh()
        self.sigmaprime = None
        self.delta_hl = None
        self.bin_variances = None
        self._summation_matrix = self.create_summation_matrix()
        self._bin_counts = flex.double(self.Ih_table.size, 1.0) * self.summation_matrix
        self.refined_parameters = [1.0, 0.0]

    def __str__(self):
        return "\n".join(
            (
                "",
                "Error model details:",
                "  Type: basic",
                "  Current parameters: a = %.5f, b = %.5f"
                % (abs(self.refined_parameters[0]), abs(self.refined_parameters[1])),
                "  Error model formula: "
                + u"\u03C3"
                + "'"
                + u"\xb2"
                + " = a("
                + u"\u03C3\xb2"
                " + (bI)" + u"\xb2" + ")",
                "",
            )
        )

    def minimisation_summary(self):
        """Output a summary of model minimisation to the logger."""
        header = ["Intensity range", "n_refl", "variance(norm_dev)"]
        rows = []
        bin_bounds = ["%.2f" % i for i in self.binning_info["bin_boundaries"]]
        for i, (bin_var, n_refl) in enumerate(
            zip(self.binning_info["bin_variances"], self.binning_info["refl_per_bin"])
        ):
            rows.append(
                [
                    bin_bounds[i] + " - " + bin_bounds[i + 1],
                    str(n_refl),
                    str(round(bin_var, 3)),
                ]
            )
        st = simple_table(rows, header)
        logger.info(
            "\n".join(
                (
                    "Intensity bins used during error model refinement:",
                    st.format(),
                    "variance(norm_dev) expected to be ~ 1 for each bin.",
                    "",
                )
            )
        )

    @property
    def summation_matrix(self):
        """A sparse matrix to allow summation over intensity groups."""
        return self._summation_matrix

    @property
    def bin_counts(self):
        """An array of the number of intensities assigned to each bin."""
        return self._bin_counts

    def filter_large_deviants(self, cutoff=6.0):
        """Do a first pass to calculate delta_hl and filter out the largest
        deviants, so that the error model is not misled by these and instead
        operates on the central ~90% of the data."""
        self.n_h = self.Ih_table.calc_nh()
        self.Ih_table.calc_Ih()
        self.sigmaprime = self.calc_sigmaprime([1.0, 0.0])
        delta_hl = self.calc_deltahl()
        sel = flex.abs(delta_hl) < cutoff
        # also filter groups with Ih < 2.0
        sel2 = self.Ih_table.Ih_values > 2.0
        self.Ih_table = self.Ih_table.select(sel & sel2)
        self.n_h = self.Ih_table.calc_nh()

    def calc_sigmaprime(self, x):
        """Calculate the error from the model."""
        sigmaprime = (
            x[0]
            * ((self.Ih_table.variances) + ((x[1] * self.Ih_table.intensities) ** 2))
            ** 0.5
        ) / self.Ih_table.inverse_scale_factors
        return sigmaprime

    def calc_deltahl(self):
        """Calculate the normalised deviations from the model."""
        I_hl = self.Ih_table.intensities
        g_hl = self.Ih_table.inverse_scale_factors
        I_h = self.Ih_table.Ih_values
        prefactor = ((self.n_h - flex.double(self.n_h.size(), 1.0)) / self.n_h) ** 0.5
        delta_hl = prefactor * ((I_hl / g_hl) - I_h) / self.sigmaprime
        return delta_hl

    def update_for_minimisation(self, x):
        """"Calculate the updated quantites."""
        self.sigmaprime = self.calc_sigmaprime(x)
        self.delta_hl = self.calc_deltahl()
        self.bin_variances = self.calculate_bin_variances()

    def create_summation_matrix(self):
        """"Create a summation matrix to allow sums into intensity bins.

        This routine attempts to bin into bins equally spaced in log(intensity),
        to give a representative sample across all intensities. To avoid
        undersampling, it is required that there are at least 100 reflections
        per intensity bin unless there are very few reflections."""
        n = self.Ih_table.size
        self.binning_info["n_reflections"] = n
        summation_matrix = sparse.matrix(n, self.n_bins)
        scaled_I = self.Ih_table.intensities / self.Ih_table.inverse_scale_factors
        size_order = flex.sort_permutation(scaled_I, reverse=True)
        Imax = max(scaled_I)
        Imin = max(1.0, min(scaled_I))  # avoid log issues
        spacing = (log(Imax) - log(Imin)) / float(self.n_bins)
        boundaries = [Imax] + [
            exp(log(Imax) - (i * spacing)) for i in range(1, self.n_bins + 1)
        ]
        boundaries[-1] = min(scaled_I) - 0.01
        self.binning_info["bin_boundaries"] = boundaries
        self.binning_info["refl_per_bin"] = []

        n_cumul = 0
        min_per_bin = min(100, int(n / (3.0 * self.n_bins)))
        for i in range(len(boundaries) - 1):
            maximum = boundaries[i]
            minimum = boundaries[i + 1]
            sel1 = scaled_I <= maximum
            sel2 = scaled_I > minimum
            sel = sel1 & sel2
            isel = sel.iselection()
            n_in_bin = isel.size()
            if n_in_bin < min_per_bin:  # need more in this bin
                m = n_cumul + min_per_bin
                if m < n:  # still some refl left to use
                    idx = size_order[m]
                    intensity = scaled_I[idx]
                    boundaries[i + 1] = intensity
                    maximum = boundaries[i]
                    minimum = boundaries[i + 1]
                    sel = sel1 & (scaled_I > minimum)
                    isel = sel.iselection()
                    n_in_bin = isel.size()
            self.binning_info["refl_per_bin"].append(n_in_bin)
            for j in isel:
                summation_matrix[j, i] = 1
            n_cumul += n_in_bin

        return summation_matrix

    def calculate_bin_variances(self):
        """Calculate the variance of each bin."""
        sum_deltasq = (self.delta_hl ** 2) * self.summation_matrix
        sum_delta_sq = (self.delta_hl * self.summation_matrix) ** 2
        bin_vars = (sum_deltasq / self.bin_counts) - (
            sum_delta_sq / (self.bin_counts ** 2)
        )
        self.binning_info["bin_variances"] = bin_vars
        return bin_vars

    def update_variances(self, variances, intensities):
        """Use the error model parameter to calculate new values for the variances."""
        new_variance = (self.refined_parameters[0] ** 2) * (
            variances + ((self.refined_parameters[1] * intensities) ** 2)
        )
        return new_variance

    def clear_Ih_table(self):
        del self.Ih_table
