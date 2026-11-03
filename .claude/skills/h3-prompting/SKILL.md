---
name: h3-prompting
description: Write, rewrite, audit, and debug MiniMax H3 (Hailuo 3) video prompts for T2VA, I2VA, FL2VA, L2VA, and Ref2VA, on local ComfyUI or hosted APIs. Use this whenever a task involves H3 or Hailuo prompts, shot timelines, dialogue or lyric tags, keyframe alignment, reference images, video or audio, or bad H3 output such as mumbling, identity drift, garbled text, or ignored actions, even if the user only says "write a prompt for this shot".
---

# MiniMax H3 Prompting

H3 generates picture and stereo sound in one pass from a structured, timed description. MiniMax's checkpoints expect the output of its hosted rewriter, H3-Context-IR, and its model card calls that step critical to quality. **Local generation skips Context-IR, so your prompt must be written in the rewriter's final form.**

Rules here come from MiniMax's guides and API docs unless tagged:
- `[community]`: repeated practitioner advice, not controlled tests.
- `[inferred]`: a conclusion drawn from official material, not stated by it.

Read MiniMax's own guide for edge cases: [base modes](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/base-en.txt) or [Ref2VA](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/references/ref-en.txt).

## Workflow

1. Pick the mode and the target runner.
2. Set the exact duration. Locally, choose the frame count first.
3. Write the alignment line or reference sections that the mode needs.
4. Write the timeline in playback order, then the two audio fields.
5. Run the pre-flight check.
6. Change one variable per retry, and iterate at 768p on short clips before spending on long ones.

## Choose the mode

| Mode | Inputs | Use it for | Local checkpoint and node |
|---|---|---|---|
| T2VA | Text | A scene from nothing | FL2VA, `MiniMaxH3ImageToVideo` with no frames |
| I2VA | First frame | Animating a strong still | FL2VA, `first_frame` |
| FL2VA | First and last frame | A designed move between two states | FL2VA, both frames |
| L2VA | Last frame | Landing on an exact ending | FL2VA, `last_frame` |
| Ref2VA | Images, video, audio | Identity, motion, voice, editing, audio reuse | Ref2VA, `MiniMaxH3ReferenceToVideo` |

The FL2VA and Ref2VA checkpoints are separate models and are not interchangeable.

Hosted requests cannot mix `first_frame` or `last_frame` with reference roles. In Ref2VA, send the image as a reference and define it as `<Picture N>`, the first frame of `[Shot 1]`, because the Ref2VA guide supports frame anchors that way.

## Write what can be seen and heard

- Write the body in English. Keep dialogue, lyrics, and visible text in their original language.
- Tie every detail to something visible or audible, in playback order.
- Prefer physical actions to intentions: "she lowers her gaze and her shoulders drop", not "she feels abandoned".
- Use a style label such as `Live-action, cinematic` or `3D CG` once at the opening, then describe the actual light, lens, and materials. A label does not replace those details.
- State the wanted behaviour instead of listing bans. The checkpoints are CFG-distilled, and ComfyUI runs H3 at CFG 1 with no negative branch, so ban lists can introduce the concepts they name.
- Be specific and long enough. MiniMax's three published Context-IR rewrites run 378 to 723 words, and the Ref2VA guide targets 350 to 500 words for `detailed_description`. Short prompts succeed on hosted endpoints partly because those can expand them `[inferred]`.
- Give each clip one job `[community]`. Crowded clips with several speakers, actions, and camera moves degrade picture or sound.

## Base format (T2VA, I2VA, FL2VA, L2VA)

The fields appear in this order, in plain text. Keyframe modes add one alignment line and one blank line first.

```text
integrated_multimodal_description: [Shot 1] <style and opening composition> ... [Shot 2] At 00:03.500, the camera cuts to ...

overall_soundscape: ...

non_diegetic_music: ...
```

### Alignment lines

Copy these exactly. `N` is the final shot number, and `S.SS` is the real clip duration to two decimals.

| Mode | First line |
|---|---|
| T2VA | None |
| I2VA | `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.` |
| FL2VA | `How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.` |
| L2VA | `How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.` |

### Build the path

- **I2VA:** first-frame anchor, action onset, continuous development, result.
- **FL2VA:** first-frame state, observable intermediate changes, narrowing differences, last-frame state. Favour one shot. Describe the motion between the frames rather than restating two stills.
- **L2VA:** plausible earlier state, explicit action path, gradual convergence, last-frame landing. The picture belongs to the final shot.

## Shots and cuts

- `[Shot 1]` has no timestamp. Later shots start `[Shot N] At MM:SS.mmm, the camera cuts to ...`, with strictly increasing times inside the clip.
- Cut verbs are `the camera cuts to`, `the shot cuts to`, `the shot transitions to`, `the shot changes to`, and `the shot switches to`. Use a dissolve, fade, or wipe only when it is explicitly wanted.
- Cut only for new subject, space, state, viewpoint, or time. Use camera motion for a small framing change.
- Default to 1 to 3 shots in a 4 to 15 second clip, or 2 to 5 second shots for music edits `[community]`. MiniMax's music-video workflow also uses 2 to 5 seconds.

## Camera

Write one sentence: motion type, then amplitude and speed only when they matter. Omit medium amplitude and normal speed. One dominant move per shot is safest `[community]`.

| Motion | Meaning |
|---|---|
| Zoom In / Out | Focal length changes, camera stays |
| Push In / Pull Out | Camera moves forward or back |
| Pan Left / Right, Tilt Up / Down | Lens pivots in place |
| Truck Left / Right, Pedestal Up / Down | Whole camera translates |
| Arc Shot, Tracking Shot | Circles a subject, follows a moving subject |
| Static Shot, POV | Holds still, subject's viewpoint |
| Shake Slightly / Strongly, Roll Clockwise / Counterclockwise | Shake, rotation around the lens axis |

Amplitude is `with small amplitude` or `with large amplitude`. Speed is `at slow speed` or `at fast speed`.

```text
The camera pushes in with small amplitude at slow speed toward the folded letter in her hands.
```

## Voices, dialogue, and text

- Number each source that speaks or sings as `(S1)`, `(S2)`, by first vocal event in the target video. Keep the ID across shots. Characters who never vocalize get no ID. Use `(S1,S2)` for voices together. `(S1)` is unrelated to `<Subject 1>`.
- Introduce each new speaker with visual identity plus pitch, timbre, pace, or accent outside the tag. Put only the language tag and exact words inside:

```text
The young woman with a quiet, breathy voice (S1) says: <d>[English] I get off at the next station.</d>
```

- Voiceover uses the exact phrase `says in an off-screen voiceover`, immediately followed by a statement that the on-screen character's lips remain closed.
- For speech across a cut, put `<scenetrans>` at the connecting point of both parts and state that the audio continues across the cut. Use `<cutoff>` when the clip ends mid-speech. The guide shows no tag example, so test placement inside `<d>` `[inferred]`.
- Quote visible text exactly in English double quotes, without translating: `A red neon sign reading "营业中" glows above the doorway.` Short strings are safest, and spelling text out beats vague phrases like "HUD elements" `[community]`.
- Stable dialogue languages are Arabic, Chinese, English, French, German, Italian, Japanese, Korean, Portuguese, Russian, and Spanish.

## Audio fields

- `overall_soundscape`: 1 to 4 sentences of ambience, physical sounds, and non-verbal human sounds. Leave out dialogue, singing, and music. Use `N/A` only for deliberate complete silence.
- `non_diegetic_music`: 1 to 3 sentences of instrumentation, tempo, rhythm, and dynamics, without mood words. Use `N/A` when there is none. Radio, performance, and other music the characters can hear goes in the timeline.
- Write `N/A` explicitly when you want no score. Leaving music unmentioned can invite a generated soundtrack `[community]`.

## Ref2VA format

Use six sections in this order. This is the standard Ref2VA form, not an advanced option.

```text
subject_definitions:
<Subject 1> is the young woman in <Picture 1>, with long dark hair and a blue cardigan.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1).

summary:
[reference generation + audio reference] One short paragraph using only labels defined above.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - face, hair, and cardigan are retained.
<Audio 1>: reference - follows its voice timbre without copying the signal.

detailed_description:
The target video is in a <style>, stated in one or two sentences before Shot 1.
[Shot 1] ...
[Shot 2] At 00:04.000, the shot cuts to ...

overall_soundscape:
...

non_diegetic_music:
...
```

### Labels

| Label | Use it for |
|---|---|
| `<Subject N>` | Reusable content such as a person, object, scene, costume, style, or action. One subject can draw on several assets, and each should have a stated job. |
| `<Picture N>` | An image acting as a concrete frame, keyframe, storyboard, or composition anchor. An image that only defines a subject is cited inside that subject's definition. |
| `<Video N>` | A whole-video relationship: source to edit, continuation start, or camera, cut, and rhythm structure. |
| `<Audio N>` | An audio signal that is copied or referenced. A video's sound gets a label only when that track is actually used. |

Each media type is numbered separately, and a label keeps one meaning across every section. Give each reference a narrow job and say what to ignore, such as "motion only, not environment".

### Fixed values

- **Summary task prefix, joined with ` + `:** `keyframe completion`, `reference generation`, `video editing`, `video continuation`, `audio reuse`, `audio reference`.
- **Visual retention markers:** `fully_preserved`, `partially_preserved`, `attribute_transfer`, `weak_reference`.
- **Audio retention markers:** `fully_copy`, `partially_copy`, `reference`, `weak_reference`.

### Speakers and reused audio

- A subject that speaks is written `<Subject N> (Sx)`. Do not write `(Sx)` in `retention_analysis`.
- Words that exist only inside reused music are cued from `<Audio N>` and get no new speaker: `When <Audio 1> reaches the phrase <d>[English] ...</d>, <Subject 1> raises a hand.`
- If only timbre or rhythm is referenced, do not carry over the reference's words.

### Hosted natural-language prompts

Hosted reference nodes and vendors accept instructions like `Use Image 1 only for the face`. That is input for an expanding or rewriting pipeline, not the final form, and vendors do not agree on a syntax. For the MiniMax-H3 endpoint, the docs define no reference syntax in prompt text, so prefer the six-section form when you want control `[inferred]`.

## Runner constraints

| | Local ComfyUI | Hosted MiniMax API |
|---|---|---|
| Rewriting | None | Context-IR is a separate endpoint. `MiniMax-H3-Max` also has `prompt_expansion_mode`: `disabled`, `balanced` (default), or `quality`. |
| Prompt limit | Not documented | 7,000 characters per text item. Regeneration allows 40,000. |
| Duration | Frames on the 17k+5 grid at 24 fps | 4 to 15 s. `MiniMax-H3-Max` is 5 to 15 s. |
| Resolution | 1344x768 at 16:9, multiples of 32. Avoid the 1376x768 step. | `MiniMax-H3`: 768P or 2K. Highspeed, Lite, and Max: 480P or 768P. |
| 2K | Not available | Regeneration only, see below |

**Local frame grid.** Use `frames / 24` for timestamps and `S.SS`:

| Frames | 107 | 124 | 141 | 158 | 175 | 192 | 209 | 226 |
|---|---|---|---|---|---|---|---|---|
| Seconds | 4.46 | 5.17 | 5.88 | 6.58 | 7.29 | 8.00 | 8.71 | 9.42 |

| Frames | 243 | 260 | 277 | 294 | 311 | 328 | 345 | 362 |
|---|---|---|---|---|---|---|---|---|
| Seconds | 10.13 | 10.83 | 11.54 | 12.25 | 12.96 | 13.67 | 14.38 | 15.08 |

The ComfyUI node states a trained range of about 124 to 362 frames, so 107 is outside it.

**Local nodes.**
- Connect `vae` and `audio_vae` to `MiniMaxH3ReferenceToVideo`. Without them, references only condition the text encoder.
- `ref_image_size: max` favours identity fidelity but can be several times slower than `match`.
- MiniMax's FL2VA ComfyUI example uses 20 steps. Turbo LoRAs use 8 steps for FL2VA and 4 for Ref2VA, and MiniMax warns they can reduce motion and audio quality. Judge prompts at full steps when a result looks weak.
- The pruned, quantized ComfyUI files are a different numerical baseline from the official checkpoint, so results do not transfer exactly.

**Reference limits.** Up to 9 images, 3 videos, and 3 audio clips, with 12 files in total. Video and audio clips run 2 to 15 s, with 15 s total per type. Audio needs at least one image or video reference. On image-to-video requests the ratio follows the input image.

**2K regeneration.** This is a separate 768P-to-2K step, not a simple upscale setting.
- Send the final prompt the model actually received, which means the Context-IR rewrite if one was used.
- Resubmit every reference exactly as used.
- The base video must keep its audio track and be 24 fps, 107 to 362 frames.

## Music video work

- Treat one master song as the audio timeline. Generate clips against it and align them in the edit, rather than trusting separately generated music `[community]`. MiniMax's music-video workflow does the same.
- Cut on beats, breaths, or lyric pauses, never inside an active vowel.
- Set `non_diegetic_music: N/A` when clip audio will be discarded.
- For performed lyrics locked to the song, use Ref2VA: define the song slice as `<Audio N>`, mark it `fully_copy`, and write the singer as `<Subject N> (S1)` with the exact words in `<d>`. This follows the guide's labels, but check it on a short clip first `[inferred]`.
- Keep the lyric text verbatim. Any visible lyric must match the sung line word for word, and text should not cover the eyes or mouth.
- Expect less reliable lip-sync on non-human faces `[community]`. One vendor test reports inconsistent triggering, so test animal characters early.

## Pre-flight check

- [ ] The clip has one clear job, and the mode and runner are chosen.
- [ ] Field names, section order, and any alignment line are exact.
- [ ] Style and opening composition come first, in the right place for the mode.
- [ ] Actions are physical and sequenced, and no sentence depends on mood words.
- [ ] Timestamps increase and fit the real duration. Locally, duration is `frames / 24`.
- [ ] Every vocal source has a stable `(Sx)`, and words sit inside `<d>[Language] ...</d>`.
- [ ] Sound fields are filled, with `N/A` only where intended.
- [ ] Every Ref2VA label is defined, has one job, and is used consistently.
- [ ] Prompt length fits the runner.

## Templates

Fill these rather than copying their subject matter. They show the structure at reduced length, so add composition, subject, light, and sound detail. The Ref2VA `detailed_description` here is 168 words against a normal target of 350 to 500.

### T2VA, two-shot dialogue

```text
integrated_multimodal_description: [Shot 1] Live-action, natural window light, a medium shot frames two coworkers at a small office kitchen table, in a static shot. The man in a grey sweater with a calm mid-range voice (S1) slides a laptop across the table and says: <d>[English] The numbers finally add up.</d> He closes his mouth and leans back. [Shot 2] At 00:05.000, the camera cuts to a close-up of the woman with short black hair, who reads the screen with her lips closed, then looks up and smiles. The woman with a dry, low voice (S2) says: <d>[English] Then we ship on Monday.</d>

overall_soundscape: A fridge hums beneath distant keyboard clicks from the next room. The laptop scrapes softly across the table.

non_diegetic_music: N/A
```

### I2VA, animate a still

```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, the young woman shown in <Picture 1> remains beside the rain-covered train window at night, preserving her appearance, clothing, seat position, and the carriage layout. The camera trucks right with small amplitude at slow speed as she unfolds a letter, reads the first line, and raises her eyes toward the passing city lights. The young woman with a quiet, breathy voice (S1) says: <d>[English] I get off at the next station.</d>

overall_soundscape: Train wheels keep a steady metallic rhythm beneath a low ventilation hum. Rain ticks against the window and paper rustles softly.

non_diegetic_music: Sparse piano notes at a slow tempo over sustained low strings, gradually decreasing in volume.
```

### FL2VA, one designed move (8.00 s, 192 frames)

```text
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) aligns with the 8.00-second mark of the target video.

integrated_multimodal_description: [Shot 1] 3D CG, a small dog begins in the position and framing established by Picture 1, seated at the left end of a garden wall. The camera trucks right with small amplitude at slow speed as the dog stands, walks along the wall with its tail raised, stops at a terracotta pot, and turns toward the lens, settling into the pose, spacing, and composition established by Picture 2 at the end of the shot.

overall_soundscape: Light paw taps cross warm brick while sparrows chirp in a nearby hedge. Distant traffic passes under a soft breeze.

non_diegetic_music: N/A
```

### Ref2VA, identity and voice

```text
subject_definitions:
<Subject 1> is the older fisherman in <Picture 1> and <Picture 2>, with a white beard, a weathered face, and a yellow oilskin coat.
<Subject 2> is the small harbour with stone steps and moored wooden boats in <Picture 3>.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1).

summary:
[reference generation + audio reference] <Subject 1> coils a rope on the stone steps of <Subject 2>, looks up at the sky, and speaks one line using the voice timbre of <Audio 1>.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - his face, white beard, and yellow oilskin coat are retained.
<Subject 2> (appears in [Shot 1]): partially_preserved - the steps and boats are retained, while the weather changes to low grey cloud.
<Audio 1>: reference - his delivery follows the timbre of <Audio 1> without copying the original signal or its words.

detailed_description:
The target video is live-action with an overcast, low-contrast look and a muted blue-grey palette.
[Shot 1] A medium-wide shot frames <Subject 2> under low grey cloud, with wooden boats rocking at their moorings in the background. <Subject 1>, the older fisherman with the white beard and yellow oilskin coat, sits on the third stone step in the left third of the frame, looping wet rope around his forearm. The camera pushes in with small amplitude at slow speed as he finishes the last loop, drops the coil beside his boot, and rests both hands on his knees. Water runs from the rope across the stone.
[Shot 2] At 00:05.000, the shot cuts to a close-up of <Subject 1>, whose eyes lift toward the sky as wind moves his beard. <Subject 1> (S1), in the deep, gravelly voice referenced from <Audio 1> and at an unhurried pace, says, <d>[English] She'll turn by morning.</d> He closes his mouth and keeps looking upward, his jaw still, as the shot holds.

overall_soundscape:
Small waves slap the stone steps while wooden hulls knock softly against their moorings. Wet rope creaks and wind moves across the harbour.

non_diegetic_music:
N/A
```

## Diagnose failures

Change one thing at a time. Most of these fixes are `[community]`, so treat them as hypotheses.

| Symptom | Likely cause | Try |
|---|---|---|
| Mumbling or lip movement on a silent character | Character is visible and voice ownership is ambiguous | Add "remains silent with her mouth closed", and frame the speaker alone |
| Gibberish speech | Mixed languages, a long line, unusual names, or too little time | Use one language tag and one short line per clip, then match duration to the line |
| Invented voices or score | Empty or vague audio intent | Fill both sound fields, with `N/A` for no score |
| Faces blend, or the wrong person speaks | Similar characters share a frame | Add physical anchors, give each speaker a single-subject shot, and keep speaker IDs stable |
| Action is skipped | Framing is too wide, or too much is packed in | Move closer, sequence the steps, and cut the action count |
| Camera wanders | Several moves or tag-style phrases | Write one move as a sentence, or a static shot |
| Identity drift | Overlapping or vague references | Give each asset one job, add reference angles, and locally try `ref_image_size: max` |
| Garbled text | Text is vague or long | Quote the exact string and shorten it |
| Weak result on a Turbo run | Low step count | Compare at full steps before rewriting the prompt |
| Wrong last frame or timing | The stated duration differs from the real frame count | Recompute `S.SS` and timestamps from frames |

## Sources

- [MiniMax H3 model card](https://huggingface.co/MiniMaxAI/MiniMax-H3) and [official skills](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills)
- [Video API](https://platform.minimax.io/docs/api-reference/video-generation-v2-create), [Context-IR API](https://platform.minimax.io/docs/api-reference/video-generation-v2-h3-context-ir), [2K regeneration API](https://platform.minimax.io/docs/api-reference/video-generation-v2-regeneration), [local deployment guide](https://platform.minimax.io/docs/guides/local-deploy-h3.md)
- [ComfyUI prompt guide](https://docs.comfy.org/tutorials/video/minimax/minimax-h3-prompt-guide), [ComfyUI hosted workflows](https://docs.comfy.org/tutorials/partner-nodes/minimax/minimax-h3/workflow.md)
- Community: [fal guide](https://fal.ai/learn/devs/minimax-h3-prompting-guide), [Atlas Cloud on official prompts](https://www.atlascloud.ai/hi/blog/guides/minimax-h3-prompt-guide), [Atlas Cloud on dialogue](https://www.atlascloud.ai/blog/tips/minimax-h3-chinese-dialogue-accuracy), [Seedance on gibberish](https://www.seedance.tv/blog/minimax-h3-gibberish-fix)
