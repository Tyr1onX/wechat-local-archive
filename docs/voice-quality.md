# Optional voice verification

## Scope

SenseVoice remains the default. The optional second opinion uses faster-whisper small, CPU/int8, two threads and beam size 1. The large-v3-turbo alias is available for an explicit experiment, but has not been established as more accurate for this archive. No LLM adjudication or invented confidence score is used.

The automatic candidate rules are deliberately narrow: missing/unrecognized text, repeated text, or at most three non-punctuation characters for a voice whose XML duration is at least five seconds. These flags identify possible problems, not measured transcription errors. The default limit is 20; explicit IDs or a date range can select other voices. Automatic verification never changes the displayed transcript.

## Local benchmark, 2026-09-07

Eight deterministically sampled existing voices, about 49.3 seconds of audio, were compared on the same Windows machine. Both models were run locally in a fresh temporary transcript cache at the background resource setting. The measured times include model initialization and decoding; they are not steady-state throughput measurements.

| Measurement | Result |
| --- | ---: |
| SenseVoiceSmall ONNX | 34.32 s |
| faster-whisper small | 19.14 s |
| Text agreement, median SequenceMatcher ratio | 0.723 |
| Text agreement, mean SequenceMatcher ratio | 0.674 |

There is no human-corrected reference transcript for this sample. Agreement between two models is not word-error rate or proof that either model is correct. A previous warm 64-voice benchmark also showed that SenseVoice benefits substantially from batching; this small cold comparison does not justify changing the default full-history pipeline. We therefore retain SenseVoice for bulk ASR and choose the already-cached, lower-resource Whisper small for optional review. No large-v3-turbo benchmark or download was performed.

## Provenance and recovery

Schema 4 adds optional `transcript_source` and `transcript_reviews` fields to each message. Each review records its exact model/cache key, result, baseline text, baseline source and SHA-256 of the original audio. The first result is retained when a later review is explicitly applied. Missing source metadata in older archives means unknown provenance, not proof of a particular model.

Review cache keys include model identity/revision and decoding settings. Repeating a review of unchanged audio reuses the stored result; choosing another model does not overwrite it. Empty second-model output is recorded as empty and cannot be applied as recognized speech. Multiple stored versions require an exact review key before applying. A review can be applied without loading a model when its stored result is unambiguous.

A standalone review only reads the existing archive and local audio. A cancellation leaves the committed archive unchanged; complete model results may remain in the content-addressed cache for resumption. A successful review updates archive.json atomically and regenerates the text, AI and HTML derivatives. Ordinary incremental updates retain transcripts and review metadata for unchanged messages. An explicit `--refresh` still rebuilds source/media and reloads primary ASR from its cache, but retains historical review records for unchanged messages. A previously selected second-model result is not silently reselected by a full rebuild.

The base Windows lock is unchanged. `requirements-win-verify.lock` is an optional superset constrained by the base lock, including faster-whisper 1.2.1 and CTranslate2 4.8.2. Model weights are not bundled. Without `--download-model`, model resolution is local-only; `--model-dir` can reuse an existing converted model. Audio is never submitted to a remote inference service. No real messages, audio, keys or transcript samples are committed to this repository.
