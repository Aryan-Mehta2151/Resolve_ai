import re


def normalize_e164_phone_number(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    if not re.fullmatch(r"\+[0-9()\s.-]+", trimmed):
        raise ValueError("Phone number must include a country code in E.164 format")
    digits = re.sub(r"\D", "", trimmed)
    if not 8 <= len(digits) <= 15:
        raise ValueError("Phone number must contain 8 to 15 digits")
    return f"+{digits}"
