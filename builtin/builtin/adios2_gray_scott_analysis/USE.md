# Usage

Add `builtin.adios2_gray_scott_analysis` after two Gray-Scott simulations have completed. Configure:

- `low_input` and `high_input` as the exact absolute BP collection paths.
- `low_configuration` and `high_configuration` as absolute JSON files for the installed profile, or as manifest-relative `scientific_input` members for the source-bundle profile.
- `active_threshold` as the V concentration threshold used to classify active cells.
- `output_file` as the JSON comparison destination.
- `input_bundle` only when building the analyzer from verified source.

JARVIS validates both datasets and configurations before launch. Successful process exit is accepted only when the analyzer produces the closed `jarvis.gray-scott-morphology.v1` result bound to both input configurations and the requested threshold.
