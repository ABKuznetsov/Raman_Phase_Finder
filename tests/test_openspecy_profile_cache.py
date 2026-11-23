from contextlib import ExitStack
import gc
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from vibrational_finder.core.models import CandidateRecord, SignalKind
from vibrational_finder.services.openspecy_profile_cache import profile_cache_path
from vibrational_finder.services.openspecy_source import OpenSpecyLibrarySource
from vibrational_finder.services.reference_cache import ReferenceSpectrumCache


class OpenSpecyProfileCacheTests(unittest.TestCase):
    def test_another_card_can_open_after_restart_without_decoding_rds(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(TemporaryDirectory()))
            stack.callback(gc.collect)  # Release legacy index SQLite connections before Windows cleanup.
            archive = root / "medoid_derivative.rds"
            archive.write_bytes(b"test archive revision one")
            records = [CandidateRecord(
                key=f"OpenSpecy:{sample}", source="OpenSpecy", entry_id=sample,
                name=sample, kind=SignalKind.RAMAN,
                metadata={"library": "medoid_derivative", "sample_name": sample},
            ) for sample in ("first", "second")]
            ReferenceSpectrumCache(root).upsert_records(records)
            decoded = {
                "metadata": pd.DataFrame(),
                "wavenumber": np.array([300., 100., 200.]),
                "spectra": pd.DataFrame({"first": [0.3, 0.1, 0.2], "second": [3., 1., np.nan]}),
            }
            with patch.object(OpenSpecyLibrarySource, "_read_with_rdata", return_value=decoded) as read:
                first = OpenSpecyLibrarySource(root).load_spectrum(records[0])
                self.assertEqual(first.y, [0.1, 0.2, 0.3])

                second = OpenSpecyLibrarySource(root).load_spectrum(records[1])
                self.assertEqual(second.x, [100., 300.])
                self.assertEqual(second.y, [1., 3.])
                self.assertEqual(read.call_count, 1)

                archive.write_bytes(b"test archive revision two with a different size")
                old = archive.stat()
                os.utime(archive, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))
                decoded["spectra"]["second"] = [30., 10., 20.]
                updated = OpenSpecyLibrarySource(root).load_spectrum(records[1])
                self.assertEqual(updated.y, [10., 20., 30.])
                self.assertEqual(read.call_count, 2)

                reopened = OpenSpecyLibrarySource(root).load_spectrum(records[1])
                self.assertEqual(reopened.y, updated.y)
                self.assertEqual(read.call_count, 2)

                profile_cache_path(archive).write_bytes(b"damaged derived cache")
                rebuilt = OpenSpecyLibrarySource(root).load_spectrum(records[1])
                self.assertEqual(rebuilt.y, updated.y)
                self.assertEqual(read.call_count, 3)
                OpenSpecyLibrarySource(root).load_spectrum(records[1])
                self.assertEqual(read.call_count, 3)


if __name__ == "__main__":
    unittest.main()
