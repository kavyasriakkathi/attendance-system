import ast
import os

def check_name_errors(filename):
    with open(filename, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read())
    
    defined_names = set()
    # Add builtins
    import builtins
    defined_names.update(dir(builtins))
    
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defined_names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                defined_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                defined_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined_names.add(target.id)

    errors = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in defined_names:
                # Check if it's a global or imported name we missed
                errors.append((node.lineno, node.id))
    
    return errors

# This is a very simplified check and will have false positives for globals/imports not detected above.
# But let's see.
try:
    errs = check_name_errors('app.py')
    # Filter out common flask globals
    flask_globals = {'app', 'request', 'session', 'url_for', 'flash', 'render_template', 'redirect', 'jsonify', 'send_file', 'abort', 'g', 'current_app'}
    errs = [(l, n) for l, n in errs if n not in flask_globals]
    print(errs)
except Exception as e:
    print(f"Error: {e}")
