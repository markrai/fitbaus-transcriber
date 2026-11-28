# Original ebook visual delivery staging (E.6B.1)

This is an operator-only staging boundary, not a publication endpoint. The Heavy Transcriber produces canonical fitbaus.epub-delivery-candidate/v1 JSON with static-rgba-png/v2 rendering policy. Stage the exact bytes with their SHA-256 content address using the container command:

```sh
python -m transcriber stage-visuals --token-stdin --candidate-id <sha256>
```

Standard input must contain the current Audiobookshelf access token followed by one newline, then the exact canonical candidate JSON (no added newline). Use the same trusted SSH/container transport as import-heavy. No credential is persisted or accepted in command arguments. There is no HTTP route or bike retrieval in this phase.

The receiver independently validates strict schema, digest, profile, quotas, PNG chunks/CRC/decompression and occurrence bounds. It authenticates the current audiobook revision, checks that the selected EPUB is an attached or primary file, streams its bytes only for SHA-256 verification, then rechecks the audiobook revision. The stored completed Large-v3 transcript must cover the entire audiobook and match the canonical transcript digest. Every candidate interval must match one actual cue or two consecutive actual cues according to its method.

Storage is a transaction in the existing background SQLite database, with one staged candidate per matching imported transcript parent, 32 MiB per candidate, 64 MiB aggregate serialized bytes and 32 candidates. Reimporting or deleting the parent invalidates the candidate via cascading deletion. Exact repeat staging is idempotent; invalid replacements leave the previous candidate intact. No automatic publication, device access, timing selection or edition verification occurs. The underlying database page quota can also reject writes; the existing transcript quota is unchanged.

The content address is integrity, not a signature. The trusted operator is responsible for generating the candidate from the original enrichment pipeline; the server does not recompute MathML rendering or ebook alignment. Timing intervals are checked against actual cues, not a guarantee the narrator discusses the image. Remote source verification is a point-in-time check, not a cross-system atomic transaction. Physical bike memory/decode qualification, live authorization at retrieval, and edition/playback approval remain required before any candidate can be exposed to a client.
