from bourse.normalize import normalize_brand, normalize_size


def test_normalize_brand_variants() -> None:
    cases = [
        ("acne", "acne studios"),
        ("Acne Studio", "acne studios"),
        (" acne jeans ", "acne studios"),
        ("ACNE STUDIOS STOCKHOLM", "acne studios"),
        ("stockholm", "acne studios"),
        ("maison", "maison margiela"),
        ("Masion Margiela", "maison margiela"),
        ("Maison Martin Margiela", "maison margiela"),
        ("MM", "maison margiela"),
        ("maison marginal", "maison margiela"),
        ("mm6 maison margiela", "mm6"),
        ("Common Projects", "common projects"),
        ("—", ""),
        (" fashion ", ""),
        ("", ""),
        (None, ""),
    ]

    for raw, expected in cases:
        assert normalize_brand(raw) == expected


def test_normalize_size_variants() -> None:
    cases = [
        ("xs/34/6", "XS"),
        ("34/6", "XS"),
        ("XXXS/30/2", "XS"),
        ("xxs/32/4", "XS"),
        ("s/36/8", "S"),
        ("38/10", "M"),
        (" l/40/12 ", "L"),
        ("42/14", "XL"),
        ("44/16", "XXL"),
        ("W30", "W30"),
        ("35", "35"),
        ("47", "47"),
        ("onesize", "ONE SIZE"),
        ("One Size", "ONE SIZE"),
        ("NO SIZE", "ONE SIZE"),
        ("", ""),
        (None, ""),
        ("petite", "petite"),
    ]

    for raw, expected in cases:
        assert normalize_size(raw) == expected
