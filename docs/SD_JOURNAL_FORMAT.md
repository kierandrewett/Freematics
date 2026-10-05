# SD journal record formats

The append-only `/QUEUE.BIN` file supports two record formats. Existing records
remain readable during upgrade; new firmware writes FQJ2 records.

All integer fields are little-endian. Both formats begin with a 12-byte header:

| Offset | Size | Field |
| --- | ---: | --- |
| 0 | 4 | Magic: `FQJ1` (`0x46514A31`) or `FQJ2` (`0x46514A32`) |
| 4 | 2 | Serialized sample length |
| 6 | 2 | Reserved; must be zero |
| 8 | 4 | IEEE CRC-32 (reflected polynomial `0xEDB88320`) |

FQJ1 is followed by the serialized sample. Its CRC covers only that sample.

FQJ2 is followed by a 12-byte capture identity and then the serialized sample:

| Offset after header | Size | Field |
| --- | ---: | --- |
| 0 | 4 | Low 32 bits of the per-boot capture session |
| 4 | 4 | High 32 bits of the per-boot capture session |
| 8 | 4 | Capture sequence within that session |

The FQJ2 CRC covers the identity bytes followed by the complete serialized
sample. It therefore detects damaged identifiers as well as damaged sample
data. The sequence is allocated at the capture boundary, before buffer
allocation or SD append, so missing sequence numbers can reveal losses before
durable journaling. A session changes on device restart; the identity is stored
on SD with the sample and is independent of upload or collector receipt time.

The persistent identity is stored beside every SD replay source sample; the
same session plus sequence is exposed in FT2 live USB metadata (`;seq=...`).
Replay does not yet transmit that identity to the collector. Cloud-side
idempotent receipt/acknowledgement remains a separate integration step; this
format alone does not claim duplicate-free cloud ingestion.
