"""User-facing coverage messages shared by the UI and regression checks."""


def nonreciprocal_notice(result: dict) -> str:
    if not result["ready"]:
        return "Не взаимно: нужен новый сбор данных."
    if result["complete"]:
        return f"Не взаимно: {result['total']}."
    return (
        f"Не взаимно: подтверждено {result['total']}. "
        f"Ещё {result['hidden_count']} подписок недоступно."
    )


def nonreciprocal_empty(result: dict) -> tuple[str, str]:
    if not result["ready"]:
        return "Нужна новая проверка", "Соберите подписчиков и подписки вместе."
    if result["complete"]:
        return "Невзаимных подписок нет", "Проверен весь список подписок."
    return (
        "В доступной части невзаимных нет",
        f"Instagram не отдал {result['hidden_count']} подписок. По ним пока нет данных.",
    )
