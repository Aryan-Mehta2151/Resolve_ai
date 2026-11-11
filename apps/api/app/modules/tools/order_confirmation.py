import re


def is_order_confirmation(message: str) -> bool:
    return re.fullmatch(r"confirm\s+order[.!?]?", message.strip(), re.IGNORECASE) is not None
