def calculate_commission(amount):
    if amount < 0:
        raise ValueError("Amount cannot be negative")

    return amount * 0.06


print(calculate_commission(100))