import shlex

def command_line(words):
    # Existing implementation
    return ' '.join(shlex.quote(w) if w != '--' else '--' for w in words)

def new_command_line(words):
    # Proposed fix
    if len(words) == 1 and words[0] == '--':
        return ':'
    if len(words) > 1 and words[-1] == '--':
        return ' '.join(shlex.quote(w) for w in words[:-1])
    return ' '.join(shlex.quote(w) if w != '--' else '--' for w in words)

print(f"Old(['bash', '--']): {command_line(['bash', '--'])}")
print(f"New(['bash', '--']): {new_command_line(['bash', '--'])}")
print(f"Old(['bash', '--', 'arg']): {command_line(['bash', '--', 'arg'])}")
print(f"New(['bash', '--', 'arg']): {new_command_line(['bash', '--', 'arg'])}")
