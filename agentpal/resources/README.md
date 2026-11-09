# Vendored Unicode data (P0.6)

| File | Source | Version / date | SHA-256 of the original file |
|---|---|---|---|
| `confusables-18.0.0.txt.gz` | https://www.unicode.org/Public/security/latest/confusables.txt (UTS #39) | 18.0.0, 2026-08-06 | `6ed3ee967c9dfdf6677d563c9985182fbc50a2efb7d6059cd57b2e2ce18f5b92` (uncompressed) |
| `default_ignorable-18.0.0.txt` | `Default_Ignorable_Code_Point` lines of https://www.unicode.org/Public/latest/ucd/DerivedCoreProperties.txt | 18.0.0, 2026-08-07 | `09c928886a178fcafd93c29e4bd59073a058e5a100b716d425cb563ab50f68c9` |

Both files are © Unicode, Inc. and are redistributed under the Unicode terms of use (https://www.unicode.org/terms_of_use.html). The compressed file keeps the original header.

**Updating:**
1. Download the new files.
2. Verify their SHA-256 against what unicode.org publishes.
3. Regenerate these files and update this table.
4. Run `python3 -m pytest tests/test_p0_6_normalization.py`.

The test suite checks that the vendored confusables file still decompresses to the recorded hash.
