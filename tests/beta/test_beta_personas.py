"""Tests for beta_tools.personas."""

from __future__ import annotations


import pytest

from beta_tools.personas import load_puppet_personas


def test_load_puppet_personas_from_file(tmp_path):
    yaml_text = """\
- key: alice
  display_name: Alice
  avatar_url: https://example.com/alice.png
  activity_weight: 1.0
  channel_affinities:
    general: 0.5
    photos: 0.5
  voice_likely: true
  message_length_bias: short

- key: bob
  display_name: Bob the Builder
  avatar_url: https://example.com/bob.png
  activity_weight: 1.5
  channel_affinities:
    drama: 1.0
  voice_likely: false
  message_length_bias: medium
"""
    p = tmp_path / "puppets.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    personas = load_puppet_personas(p)
    assert len(personas) == 2
    assert personas[0].key == "alice"
    assert personas[0].display_name == "Alice"
    assert personas[0].activity_weight == 1.0
    assert personas[0].channel_affinities == {"general": 0.5, "photos": 0.5}
    assert personas[0].voice_likely is True
    assert personas[0].message_length_bias == "short"

    assert personas[1].key == "bob"
    assert personas[1].activity_weight == 1.5
    assert personas[1].voice_likely is False


def test_load_puppet_personas_rejects_bad_length_bias(tmp_path):
    yaml_text = """\
- key: alice
  display_name: Alice
  avatar_url: https://example.com/a.png
  activity_weight: 1.0
  channel_affinities: {general: 1.0}
  voice_likely: true
  message_length_bias: enormous
"""
    p = tmp_path / "puppets.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="message_length_bias"):
        load_puppet_personas(p)


def test_load_puppet_personas_rejects_duplicate_keys(tmp_path):
    yaml_text = """\
- key: alice
  display_name: Alice One
  avatar_url: https://example.com/a.png
  activity_weight: 1.0
  channel_affinities: {general: 1.0}
  voice_likely: true
  message_length_bias: short
- key: alice
  display_name: Alice Two
  avatar_url: https://example.com/a2.png
  activity_weight: 1.0
  channel_affinities: {general: 1.0}
  voice_likely: true
  message_length_bias: short
"""
    p = tmp_path / "puppets.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_puppet_personas(p)


def test_load_puppet_personas_rejects_missing_required_field(tmp_path):
    yaml_text = """\
- key: alice
  display_name: Alice
  avatar_url: https://example.com/a.png
  activity_weight: 1.0
  channel_affinities: {general: 1.0}
  voice_likely: true
  message_length_bias: short
- key: bob
  display_name: Bob
  avatar_url: https://example.com/b.png
  activity_weight: 1.0
  voice_likely: true
  message_length_bias: short
"""
    p = tmp_path / "puppets.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="persona #1 missing required field 'channel_affinities'"):
        load_puppet_personas(p)


def test_load_puppet_personas_rejects_non_dict_channel_affinities(tmp_path):
    yaml_text = """\
- key: alice
  display_name: Alice
  avatar_url: https://example.com/a.png
  activity_weight: 1.0
  channel_affinities: general
  voice_likely: true
  message_length_bias: short
"""
    p = tmp_path / "puppets.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="channel_affinities must be a mapping"):
        load_puppet_personas(p)


def test_load_puppet_personas_rejects_null_key(tmp_path):
    yaml_text = """\
- key: ~
  display_name: Alice
  avatar_url: https://example.com/a.png
  activity_weight: 1.0
  channel_affinities: {general: 1.0}
  voice_likely: true
  message_length_bias: short
"""
    p = tmp_path / "puppets.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="invalid key"):
        load_puppet_personas(p)
