import os

os.environ["LANGSMITH_TRACING"] = "false"  # unit tests must not ship traces (config only setdefaults)
