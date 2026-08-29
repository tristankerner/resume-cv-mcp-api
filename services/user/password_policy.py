import string

from pydantic import SecretStr


class PasswordPolicy:
    @staticmethod
    def validate(value: SecretStr | None) -> SecretStr | None:
        """8+ characters with upper, lower, digit and punctuation.

        Shared by every DTO that sets a password — registration, an admin
        update, a self-edit, and the change-password form — so the rule is
        enforced once rather than copied. `None` passes through unchanged: an
        update request's password field is optional (leave it as-is), and
        this validator is used there too.
        """
        if value is None:
            return value

        pw_str = value.get_secret_value()

        if len(pw_str) < 8:
            raise ValueError("Password must be at least 8 characters long.")

        if not any(c.isupper() for c in pw_str):
            raise ValueError("Password must contain at least one uppercase letter.")

        if not any(c.islower() for c in pw_str):
            raise ValueError("Password must contain at least one lowercase letter.")

        if not any(c.isdigit() for c in pw_str):
            raise ValueError("Password must contain at least one digit.")

        if not any(c in string.punctuation for c in pw_str):
            raise ValueError("Password must contain at least one special character.")

        return value
