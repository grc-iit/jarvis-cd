import pathlib,os
cwd = os.path.dirname(pathlib.Path(__file__).parent.resolve())
for file in os.listdir(cwd):
    if os.path.isfile(file):
        exec(f"from {file} import *")