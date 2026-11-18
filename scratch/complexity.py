import ast

def get_complexity(code):
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            complexity = 1
            for child in ast.walk(node):
                if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.With, ast.BoolOp)):
                    complexity += 1
            print(f"Function: {node.name}, Complexity: {complexity}")

with open('tutti/secretenv.py', 'r') as f:
    get_complexity(f.read())
