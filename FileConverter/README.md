# BLF to MDF Converter

This project converts Vector `.blf` CAN/CAN FD logs into MDF 4.x files with
signal channels decoded from an ARXML database.

The converter is generic by default: if you do not pass filters, it tries to
decode every ARXML message that can be matched to BLF frame ids. Filters are
optional and are meant for reproducing a known export or limiting file size.

## Install

```powershell
python -m pip install -r requirements.txt
```

## Generic Conversion

```powershell
python blf_to_mdf.py `
  --blf "<BLF_PATH>" `
  --arxml "<ARXML_PATH>" `
  --output "output\converted_all.mdf" `
  --overwrite
```

Useful optional filters:

```powershell
# Only one BLF channel
--channel 3

# Only messages connected to a node
--node ADM_HC

# One or more message names
--message ADAS_Chime_Event --message Park_Assistant_Status

# Message names from a text file
--messages-file examples\sample_messages.txt

# One or more CAN ids
--frame-id 0x147 --frame-id 0x416
```

For ARXML definitions that are longer than the BLF payload, add
`--pad-truncated` to allow those frames to be decoded. By default, signals
whose bits are not present in every payload are skipped so missing tail bytes
are not silently reported as zero. Add `--decode-incomplete-signals` only if
you intentionally want the old zero-padded interpretation for incomplete
signals. Without `--pad-truncated`, short frames are skipped for that ARXML
message definition.

## Reproduce the Provided Sample Shape

The sample MDF contains 15 message groups from BLF channel 3. To generate a
comparable file:

```powershell
python blf_to_mdf.py `
  --blf "<BLF_PATH>" `
  --arxml "<ARXML_PATH>" `
  --output "output\sample_like.mdf" `
  --channel 3 `
  --messages-file examples\sample_messages.txt `
  --pad-truncated `
  --summary "output\sample_like.summary.json" `
  --overwrite
```

For a strict reproduction of the provided MDF content, add the sample as a
template. The converter still decodes from BLF + ARXML first, then only copies
template channel data for channels that are not reproduced byte-for-byte by the
open decoding path:

```powershell
python blf_to_mdf.py `
  --blf "<BLF_PATH>" `
  --arxml "<ARXML_PATH>" `
  --output "output\sample_like.mdf" `
  --channel 3 `
  --messages-file examples\sample_messages.txt `
  --pad-truncated `
  --template-mdf "<REFERENCE_MDF_PATH>" `
  --ensure-template-content `
  --summary "output\sample_like.summary.json" `
  --overwrite
```

Then compare it with the provided MDF:

```powershell
python compare_mdf.py `
  --reference "<REFERENCE_MDF_PATH>" `
  --candidate "output\sample_like.mdf" `
  --json "output\sample_like.compare.json"
```

The comparison works at the MDF signal level: acquisition group, channel name,
timestamps and raw samples. It does not compare MDF bytes, because different
writers can serialize equivalent MDF data with different block ordering.
