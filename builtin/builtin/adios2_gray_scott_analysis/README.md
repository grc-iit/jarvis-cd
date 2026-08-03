# ADIOS2 Gray-Scott Analysis

`builtin.adios2_gray_scott_analysis` compares the final three-dimensional fields from two completed ADIOS2 Gray-Scott BP datasets. It reports per-field morphology and a paired V-field comparison as one validated JSON artifact.

The package can run an installed `gray-scott-analyze` executable or build only that target from a digest-verified input bundle. The bundle must declare exactly one `build_spec`, exactly one `analysis_source`, and the two selected configurations as `scientific_input` files.

This is an application package. It does not infer datasets, configurations, or thresholds, and it does not replace the Gray-Scott simulation package.
