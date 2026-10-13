import datetime as dt

from kurator.db import Photo
from kurator.dedupe import (
    BURST,
    EXACT,
    analyze,
    burst_groups,
    burst_members,
    choose_keepers,
    exact_groups,
    near_groups,
    summarize,
)


def photo(rel, *, md5=None, phash=None, dhash=None, taken=None, size=1000, w=100, h=100, video=False):
    return Photo(
        rel_path=rel,
        size=size,
        mtime=0.0,
        md5=md5,
        phash=phash,
        dhash=dhash,
        taken_at=taken,
        width=w,
        height=h,
        is_video=video,
    )


def test_exact_groups_by_md5():
    photos = [
        photo("a.jpg", md5="d1"),
        photo("b.jpg", md5="d1"),
        photo("c.jpg", md5="d2"),
    ]
    groups = exact_groups(photos)
    assert len(groups) == 1
    assert groups[0].kind == EXACT
    assert groups[0].members == ["a.jpg", "b.jpg"]


def test_exact_groups_skip_missing_md5():
    photos = [photo("a.jpg"), photo("b.jpg")]
    assert exact_groups(photos) == []


def test_choose_keepers_prefers_original_over_suffix():
    photos = [
        photo("IMG_1.jpg", md5="d", size=2000, w=400, h=400),
        photo("IMG_1 (1).jpg", md5="d", size=2000, w=400, h=400),
    ]
    groups = exact_groups(photos)
    choose_keepers(groups, photos)
    assert groups[0].keeper == "IMG_1.jpg"
    assert groups[0].victims() == ["IMG_1 (1).jpg"]


def test_choose_keepers_prefers_larger_resolution():
    photos = [
        photo("small.jpg", md5="d", size=100, w=100, h=100),
        photo("big.jpg", md5="d", size=100, w=4000, h=3000),
    ]
    groups = exact_groups(photos)
    choose_keepers(groups, photos)
    assert groups[0].keeper == "big.jpg"


def test_burst_groups_by_suffix_stem():
    photos = [
        photo("IMG_9 (0).jpg", md5="a", taken="2024-01-01T10:00:00"),
        photo("IMG_9 (1).jpg", md5="b", taken="2024-01-01T10:00:01"),
        photo("IMG_8.jpg", md5="c", taken="2024-05-05T10:00:00"),
    ]
    groups = burst_groups(photos)
    assert len(groups) == 1
    assert groups[0].kind == BURST
    assert groups[0].members == ["IMG_9 (0).jpg", "IMG_9 (1).jpg"]


def test_burst_groups_do_not_mix_two_series_in_one_folder():
    photos = [
        photo("A (0).jpg", md5="a"),
        photo("A (1).jpg", md5="b"),
        photo("B (0).jpg", md5="c"),
        photo("B (1).jpg", md5="d"),
    ]
    groups = burst_groups(photos)
    assert len(groups) == 2
    assert sorted(tuple(g.members) for g in groups) == [
        ("A (0).jpg", "A (1).jpg"),
        ("B (0).jpg", "B (1).jpg"),
    ]


def test_burst_groups_by_exif_window():
    photos = [
        photo("x.jpg", taken="2024-01-01T10:00:00"),
        photo("y.jpg", taken="2024-01-01T10:00:01"),
        photo("z.jpg", taken="2024-06-01T10:00:00"),
    ]
    groups = burst_groups(photos)
    assert len(groups) == 1
    assert groups[0].members == ["x.jpg", "y.jpg"]
    assert "похожи" in groups[0].detail["reason"]


def test_burst_needs_visual_similarity_not_only_time():
    """Последовательная съёмка не должна прятаться за burst.

    Два кадра подряд в паре секунд друг от друга — это обычная съёмка
    сцены, а не серия. Без проверки похожести на настоящем архиве так
    помечались две трети файлов.
    """
    photos = [
        photo("a.jpg", md5="1", phash="0000000000000001", taken="2024-01-01T10:00:00"),
        photo("b.jpg", md5="2", phash="ffffffffffffffff", taken="2024-01-01T10:00:02"),
    ]
    assert burst_groups(photos) == [], "кадры разные, а серия не должна из них вырасти"


def test_burst_survives_when_looking_similar():
    photos = [
        photo("a.jpg", md5="1", phash="0000000000000001", taken="2024-01-01T10:00:00"),
        photo("b.jpg", md5="2", phash="0000000000000003", taken="2024-01-01T10:00:01"),
    ]
    groups = burst_groups(photos)
    assert len(groups) == 1, "похожие кадры подряд — это серия, её надо защитить"
    assert groups[0].kind == BURST


def test_burst_without_hashes_stays_protected():
    """Видео остаётся защищённым: хешей нет, проверить похожесть нечем."""
    photos = [
        photo("v1.mov", md5="1", taken="2024-01-01T10:00:00", video=True),
        photo("v2.mov", md5="2", taken="2024-01-01T10:00:01", video=True),
    ]
    assert len(burst_groups(photos)) == 1


def test_burst_groups_exclude_outside_window():
    photos = [
        photo("x.jpg", taken="2024-01-01T10:00:00"),
        photo("z.jpg", taken="2024-01-01T10:00:09"),
    ]
    assert burst_groups(photos) == []


def test_identical_md5_is_not_treated_as_burst():
    photos = [
        photo("IMG_1.jpg", md5="same", taken="2024-01-01T10:00:00"),
        photo("IMG_1 (1).jpg", md5="same", taken="2024-01-01T10:00:00"),
    ]
    assert burst_groups(photos) == []
    assert burst_members(photos) == set()


def test_burst_never_gets_a_keeper():
    photos = [
        photo("A (0).jpg", md5="a"),
        photo("A (1).jpg", md5="b"),
    ]
    groups = burst_groups(photos)
    choose_keepers(groups, photos)
    assert groups[0].keeper is None
    assert groups[0].victims() == []


def test_near_groups_without_store_uses_phash_only():
    photos = [
        photo("a.jpg", md5="a", phash="0000000000000001"),
        photo("b.jpg", md5="b", phash="0000000000000003"),
        photo("c.jpg", md5="c", phash="ffffffffffffffff"),
    ]
    groups = near_groups(photos, distance=2, store=None)
    assert len(groups) == 1
    assert groups[0].members == ["a.jpg", "b.jpg"]


def test_summarize_counts_kinds():
    photos = [
        photo("a.jpg", md5="d"),
        photo("b.jpg", md5="d"),
        photo("c (0).jpg", md5="e"),
        photo("c (1).jpg", md5="f"),
    ]
    counts = summarize(exact_groups(photos) + burst_groups(photos))
    assert counts == {EXACT: 1, BURST: 1}

def test_near_groups_skip_byte_identical_pairs():
    """Побайтно одинаковые файлы — забота exact, второй раз их реп��ртить нельзя."""
    photos = [
        photo("a.jpg", md5="same", phash="0000000000000001"),
        photo("b.jpg", md5="same", phash="0000000000000001"),
    ]
    assert near_groups(photos, distance=2, store=None) == []


def test_near_groups_keep_different_bytes_with_same_hash():
    photos = [
        photo("a.jpg", md5="one", phash="0000000000000001"),
        photo("b.jpg", md5="two", phash="0000000000000003"),
    ]
    assert len(near_groups(photos, distance=2, store=None)) == 1


def test_analyze_protects_burst_members():
    photos = [
        photo("A.jpg", md5="a", phash="0000000000000001", taken="2024-01-01T10:00:00"),
        photo("A (1).jpg", md5="b", phash="0000000000000001", taken="2024-01-01T10:00:00"),
    ]
    groups, protected = analyze(photos, store=None, phash_distance=2, strong_cosine=0.5)
    kinds = {g.kind for g in groups}
    assert BURST in kinds
    assert not {KIND for KIND in kinds if KIND != BURST}
    assert protected == {"A.jpg", "A (1).jpg"}
