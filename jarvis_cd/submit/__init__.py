"""PSI/J submission of JARVIS pipelines.

A submitted pipeline is one PSI/J job: :mod:`jarvis_cd.core.scheduler`
builds its ``JobSpec`` from the ``scheduler:`` block and the pipeline's
node needs, and :mod:`jarvis_cd.submit.entry` is the program the job runs.
"""
