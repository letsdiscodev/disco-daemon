def strip_whitespace(value):
    if isinstance(value, str):
        value = value.strip(" \t\n\r")
    return value


PREPARERS = [strip_whitespace]
