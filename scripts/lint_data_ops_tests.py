import unittest

from lint_data_ops import lint_source


def rules_hit(source, rule):
    return [f["rule"] for f in lint_source(source, "test.py", [rule])]


class UncheckedMergeTests(unittest.TestCase):

    def test_bad(self):
        source = 'metadata.merge(batch_info, on="sample_id")'
        self.assertEqual(rules_hit(source, "unchecked-merge"), ["unchecked-merge"])

    def test_good(self):
        source = 'metadata.merge(batch_info, on="sample_id", validate="one_to_one")'
        self.assertEqual(rules_hit(source, "unchecked-merge"), [])

    def test_waived(self):
        source = 'metadata.merge(batch_info, on="sample_id")  # ANALYSIS_OK[join]: keys pre-validated'
        self.assertEqual(rules_hit(source, "unchecked-merge"), [])

    def test_bare_merge_function(self):
        source = 'from pandas import merge\nmerge(a, b, on="id")'
        self.assertEqual(rules_hit(source, "unchecked-merge"), ["unchecked-merge"])


class UnannotatedMissingnessTests(unittest.TestCase):

    def test_bad_to_numeric_coerce(self):
        source = 'df["age"] = pd.to_numeric(df["age"], errors="coerce")'
        self.assertEqual(rules_hit(source, "unannotated-missingness"), ["unannotated-missingness"])

    def test_bad_fillna(self):
        source = 'df["age"] = df["age"].fillna(0)'
        self.assertEqual(rules_hit(source, "unannotated-missingness"), ["unannotated-missingness"])

    def test_good_to_numeric_raise(self):
        source = 'df["age"] = pd.to_numeric(df["age"], errors="raise")'
        self.assertEqual(rules_hit(source, "unannotated-missingness"), [])

    def test_waived(self):
        source = 'df["age"] = df["age"].fillna(0)  # ANALYSIS_OK[imputation]: 0 is a valid count'
        self.assertEqual(rules_hit(source, "unannotated-missingness"), [])


class SentinelMaskAssignmentTests(unittest.TestCase):

    def test_bad(self):
        source = 'labeled = label_arr != ""'
        self.assertEqual(rules_hit(source, "sentinel-mask-assignment"), ["sentinel-mask-assignment"])

    def test_bad_composed(self):
        source = 'has_label = df["label"].notna() & (df["label"] != "")'
        self.assertEqual(rules_hit(source, "sentinel-mask-assignment"), ["sentinel-mask-assignment"])

    def test_good_notna(self):
        source = 'labeled = label_series.notna()'
        self.assertEqual(rules_hit(source, "sentinel-mask-assignment"), [])

    def test_good_numeric_compare_out_of_scope(self):
        source = 'nonzero = value != 0'
        self.assertEqual(rules_hit(source, "sentinel-mask-assignment"), [])

    def test_waived(self):
        source = 'labeled = label_arr != ""  # ANALYSIS_OK[missingness]: empty string is a real value here'
        self.assertEqual(rules_hit(source, "sentinel-mask-assignment"), [])

    def test_subscript_target_out_of_scope(self):
        # Documented upstream limitation: Subscript targets aren't covered.
        source = 'df["label"] = df["label"] != ""'
        self.assertEqual(rules_hit(source, "sentinel-mask-assignment"), [])


class PositionalSampleAlignmentTests(unittest.TestCase):

    def test_bad_columns(self):
        source = 'counts.columns = metadata["sample_id"]'
        self.assertEqual(rules_hit(source, "positional-sample-alignment"), ["positional-sample-alignment"])

    def test_bad_index(self):
        source = 'counts.index = metadata["barcode"]'
        self.assertEqual(rules_hit(source, "positional-sample-alignment"), ["positional-sample-alignment"])

    def test_good_non_id_key(self):
        source = 'counts.columns = config["new_columns"]'
        self.assertEqual(rules_hit(source, "positional-sample-alignment"), [])

    def test_waived(self):
        source = 'counts.columns = metadata["sample_id"]  # ANALYSIS_OK[alignment]: order guaranteed by upstream sort'
        self.assertEqual(rules_hit(source, "positional-sample-alignment"), [])


class WaiverMechanicsTests(unittest.TestCase):

    def test_waiver_forward_window_applies(self):
        source = "\n".join([
            '# ANALYSIS_OK[join]: reviewed',
            'metadata.merge(batch_info, on="sample_id")',
        ])
        self.assertEqual(rules_hit(source, "unchecked-merge"), [])

    def test_waiver_outside_window_does_not_apply(self):
        source = "\n".join(
            ['# ANALYSIS_OK[join]: reviewed'] + ["x = 1"] * 5 + ['metadata.merge(batch_info, on="sample_id")']
        )
        self.assertEqual(rules_hit(source, "unchecked-merge"), ["unchecked-merge"])

    def test_malformed_waiver_does_not_suppress(self):
        source = 'metadata.merge(batch_info, on="sample_id")  # ANALYSIS_OK no category or explanation'
        self.assertEqual(rules_hit(source, "unchecked-merge"), ["unchecked-merge"])

    def test_mismatched_category_does_not_suppress(self):
        source = "\n".join([
            'metadata.merge(batch_info, on="id")  # ANALYSIS_OK[imputation]: unrelated waiver',
            'df["age"] = df["age"].fillna(0)',
        ])
        self.assertEqual(rules_hit(source, "unchecked-merge"), ["unchecked-merge"])
        self.assertEqual(rules_hit(source, "unannotated-missingness"), [])


if __name__ == "__main__":
    unittest.main()
