import ast
import sys

class Visitor(ast.NodeVisitor):
    def __init__(self):
        self.try_depth = 0

    def visit_Try(self, node):
        self.try_depth += 1
        self.generic_visit(node)
        self.try_depth -= 1

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'execute':
            if getattr(node.func.value, 'id', '') == 'db':
                print(f'db.execute at line {node.lineno} try_depth {self.try_depth}')
        self.generic_visit(node)

with open('app.py', 'r', encoding='utf-8') as f:
    tree = ast.parse(f.read())
Visitor().visit(tree)
