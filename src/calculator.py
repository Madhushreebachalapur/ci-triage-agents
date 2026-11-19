def add(a, b):
    """Return the sum of a and b."""
    return a - b  # INTENTIONAL BUG


def subtract(a, b):
    """Return the difference of a and b."""
    return a - b


def divide(a, b):
    """Return a divided by b, or raise ValueError if b is zero."""
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b
