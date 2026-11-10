import cv2
import numpy as np
import pytest

from avatar_engine.overlay import DEFAULT_TEXT, DisclosureLabel
from avatar_engine.privacy import profile_store
from avatar_engine.privacy.consent import ConsentError, make_consent


def test_consent_required():
    with pytest.raises(ConsentError):
        make_consent({})
    with pytest.raises(ConsentError):
        make_consent({"rights_confirmed": True, "subject_is_self": False})
    r = make_consent({"rights_confirmed": True, "subject_is_self": False, "third_party_consent_reference": "doc-1"})
    assert r.third_party_consent_reference == "doc-1"


def test_profile_lifecycle(profile, isolated_data_dir):
    assert (profile.dir / "source.png").exists()
    meta = profile.meta
    assert meta["consent"]["rights_confirmed"] is True and len(meta["photo_sha256"]) == 64
    assert [p["profile_id"] for p in profile_store.list_profiles()] == [profile.profile_id]
    loaded = profile_store.load_profile(profile.profile_id)
    assert loaded.landmarks.shape == (5, 2)
    cache = isolated_data_dir / "cache"
    cache.mkdir()
    (cache / f"{profile.profile_id}_emb.npy").write_bytes(b"x")
    profile_store.delete_profile(profile.profile_id)
    assert not profile.dir.exists() and not list(cache.iterdir())
    with pytest.raises(profile_store.ProfileError):
        profile_store.load_profile(profile.profile_id)


def test_profile_rejects_bad_input(tmp_path, fake_detector):
    ok = {"rights_confirmed": True, "subject_is_self": True}
    with pytest.raises(profile_store.ProfileError):
        profile_store.create_profile(tmp_path / "x.gif", "a", ok)
    blank = tmp_path / "blank.png"
    cv2.imwrite(str(blank), np.full((400, 400, 3), 128, np.uint8))
    with pytest.raises(profile_store.ProfileError):
        profile_store.create_profile(blank, "a", ok)  # real detector: no face
    with pytest.raises(ConsentError):
        profile_store.create_profile(blank, "a", {"rights_confirmed": False}, detector=fake_detector)
    with pytest.raises(profile_store.ProfileError):
        profile_store.load_profile("../etc")


def test_label_cannot_be_emptied():
    assert DisclosureLabel(text="").text == DEFAULT_TEXT
    assert DisclosureLabel(text="hi").text == DEFAULT_TEXT
    assert DisclosureLabel(text="AI-generated avatar").text == "AI-generated avatar"
    assert DisclosureLabel(scale=0.1).scale == 1.0


def test_label_non_ascii_is_folded():
    assert DisclosureLabel(text="«Avatar generado por IA»").text == "Avatar generado por IA"
    assert DisclosureLabel(text="Avatar generado por IA — versión").text == "Avatar generado por IA version"
