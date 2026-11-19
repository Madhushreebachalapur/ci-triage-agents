class SchemaMismatchError(Exception):
    """Raised when a query references a column not present in the schema."""


SCHEMA = {"users": ["id", "username"]}


def connect():
    """Return a mock successful connection dict."""
    return {"connected": True}


def get_user_name(user_id):
    """Simulate SELECT user_name FROM users — raises SchemaMismatchError because
    the schema has 'username', not 'user_name'."""
    raise SchemaMismatchError(
        "column users.user_name does not exist (schema has: id, username)"
    )
