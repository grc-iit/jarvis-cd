import pathlib,os

def _import_recurse(root_path, root, stmts):
    for file in os.listdir(root):
        file = os.path.join(root, file)
        if os.path.isfile(file):
            file = os.path.relpath(file, root_path)
            ext = file.split('.')
            if ext[-1] == 'py':
                toks = ext[0].split('/')
                if toks[-1] == '__init__':
                    continue
                import_stmt = ".".join(toks)
                stmts.append(f"from {import_stmt} import *")
        elif os.path.isdir(file):
            _import_recurse(root_path, file, stmts)
    return stmts

def import_all(root_path, root):
    stmts = []
    _import_recurse(root_path, root, stmts)
    return "\n".join(stmts)