"""
Common Utilities
"""


def get_default_headers() -> dict:
    """
    Get default headers
    """
    return {"Content-Type": "application/json"}


def get_default_covered_components() -> tuple:
    """
    Return default covered components
    """
    return ("@method", "@target-uri")
