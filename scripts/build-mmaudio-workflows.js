#!/usr/bin/env node
// Builds two ComfyUI workflow JSONs (frontend graph format):
//   1) wan22-i2v-mmaudio.json      - native Wan2.2 14B I2V (dual high/low noise) + MMAudio Foley
//   2) add-audio-to-video-mmaudio.json - load an existing video and add MMAudio-generated sound
// Model filenames match what is on disk under /srv/ai/comfyui/models.
// fp16 MMAudio models are mandatory on Volta sm_70 (no fp8).
const fs = require('fs');
const path = require('path');

function Builder() {
  this.nodes = [];
  this.links = [];
  this.nid = 0;
  this.lid = 0;
}
Builder.prototype.node = function (type, opts) {
  opts = opts || {};
  const id = ++this.nid;
  const n = {
    id: id,
    type: type,
    pos: opts.pos || [0, 0],
    size: opts.size || [300, 120],
    flags: {},
    order: this.nodes.length,
    mode: 0,
    inputs: (opts.inputs || []).map(i => ({ name: i.name, type: i.type, link: null, ...(i.shape ? { shape: i.shape } : {}) })),
    outputs: (opts.outputs || []).map(o => ({ name: o.name, type: o.type, links: [], slot_index: undefined })),
    properties: { 'Node name for S&R': type },
    widgets_values: opts.widgets_values !== undefined ? opts.widgets_values : [],
  };
  if (opts.title) n.title = opts.title;
  this.nodes.push(n);
  return n;
};
// connect(srcNode, srcSlotIdx, dstNode, dstSlotIdx)
Builder.prototype.connect = function (src, sslot, dst, dslot) {
  const lid = ++this.lid;
  const type = src.outputs[sslot].type;
  this.links.push([lid, src.id, sslot, dst.id, dslot, type]);
  src.outputs[sslot].links.push(lid);
  src.outputs[sslot].slot_index = sslot;
  dst.inputs[dslot].link = lid;
  return lid;
};
Builder.prototype.dump = function (extraNote) {
  return {
    last_node_id: this.nid,
    last_link_id: this.lid,
    nodes: this.nodes,
    links: this.links,
    groups: [],
    config: {},
    extra: {},
    version: 0.4,
  };
};

// ---- shared MMAudio model filenames ----
const MM_MAIN = 'mmaudio_large_44k_v2_fp16.safetensors';
const MM_VAE = 'mmaudio_vae_44k_fp16.safetensors';
const MM_SYNC = 'mmaudio_synchformer_fp16.safetensors';
const MM_CLIP = 'apple_DFN5B-CLIP-ViT-H-14-384_fp16.safetensors';

// ==========================================================================
// Workflow 1: Wan 2.2 14B I2V (dual model) + MMAudio
// ==========================================================================
function buildWan() {
  const b = new Builder();
  const X = c => c * 360; // column spacing

  const loadImg = b.node('LoadImage', {
    pos: [X(0), 40], size: [320, 320],
    outputs: [{ name: 'IMAGE', type: 'IMAGE' }, { name: 'MASK', type: 'MASK' }],
    widgets_values: ['example.png', 'image'],
    title: '1. Start image',
  });
  const clip = b.node('CLIPLoader', {
    pos: [X(0), 400], size: [320, 110],
    outputs: [{ name: 'CLIP', type: 'CLIP' }],
    widgets_values: ['umt5_xxl_fp8_e4m3fn_scaled.safetensors', 'wan', 'default'],
  });
  const vae = b.node('VAELoader', {
    pos: [X(0), 540], size: [320, 60],
    outputs: [{ name: 'VAE', type: 'VAE' }],
    widgets_values: ['wan_2.1_vae.safetensors'],
  });
  const unetHi = b.node('UNETLoader', {
    pos: [X(0), 630], size: [320, 90],
    outputs: [{ name: 'MODEL', type: 'MODEL' }],
    widgets_values: ['wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors', 'default'],
    title: 'High-noise model',
  });
  const unetLo = b.node('UNETLoader', {
    pos: [X(0), 750], size: [320, 90],
    outputs: [{ name: 'MODEL', type: 'MODEL' }],
    widgets_values: ['wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors', 'default'],
    title: 'Low-noise model',
  });

  const posText = b.node('CLIPTextEncode', {
    pos: [X(1), 40], size: [360, 130],
    inputs: [{ name: 'clip', type: 'CLIP' }],
    outputs: [{ name: 'CONDITIONING', type: 'CONDITIONING' }],
    widgets_values: ['a cinematic shot, the subject moves naturally, smooth camera motion, high detail'],
    title: 'Positive prompt',
  });
  const negText = b.node('CLIPTextEncode', {
    pos: [X(1), 200], size: [360, 130],
    inputs: [{ name: 'clip', type: 'CLIP' }],
    outputs: [{ name: 'CONDITIONING', type: 'CONDITIONING' }],
    widgets_values: ['色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走'],
    title: 'Negative prompt',
  });
  const shiftHi = b.node('ModelSamplingSD3', {
    pos: [X(1), 360], size: [300, 60],
    inputs: [{ name: 'model', type: 'MODEL' }],
    outputs: [{ name: 'MODEL', type: 'MODEL' }],
    widgets_values: [8.0],
    title: 'Shift (high)',
  });
  const shiftLo = b.node('ModelSamplingSD3', {
    pos: [X(1), 450], size: [300, 60],
    inputs: [{ name: 'model', type: 'MODEL' }],
    outputs: [{ name: 'MODEL', type: 'MODEL' }],
    widgets_values: [8.0],
    title: 'Shift (low)',
  });

  const i2v = b.node('WanImageToVideo', {
    pos: [X(2), 40], size: [320, 220],
    inputs: [
      { name: 'positive', type: 'CONDITIONING' },
      { name: 'negative', type: 'CONDITIONING' },
      { name: 'vae', type: 'VAE' },
      { name: 'clip_vision_output', type: 'CLIP_VISION_OUTPUT', shape: 7 },
      { name: 'start_image', type: 'IMAGE', shape: 7 },
    ],
    outputs: [
      { name: 'positive', type: 'CONDITIONING' },
      { name: 'negative', type: 'CONDITIONING' },
      { name: 'latent', type: 'LATENT' },
    ],
    widgets_values: [832, 480, 81, 1], // width,height,length,batch_size
    title: 'WanImageToVideo (832x480, 81f)',
  });

  const kHi = b.node('KSamplerAdvanced', {
    pos: [X(3), 40], size: [320, 340],
    inputs: [
      { name: 'model', type: 'MODEL' },
      { name: 'positive', type: 'CONDITIONING' },
      { name: 'negative', type: 'CONDITIONING' },
      { name: 'latent_image', type: 'LATENT' },
    ],
    outputs: [{ name: 'LATENT', type: 'LATENT' }],
    // add_noise,noise_seed,steps,cfg,sampler,scheduler,start_at,end_at,return_leftover
    widgets_values: ['enable', 0, 20, 3.5, 'euler', 'simple', 0, 10, 'enable'],
    title: 'KSampler high (0-10)',
  });
  const kLo = b.node('KSamplerAdvanced', {
    pos: [X(4), 40], size: [320, 340],
    inputs: [
      { name: 'model', type: 'MODEL' },
      { name: 'positive', type: 'CONDITIONING' },
      { name: 'negative', type: 'CONDITIONING' },
      { name: 'latent_image', type: 'LATENT' },
    ],
    outputs: [{ name: 'LATENT', type: 'LATENT' }],
    widgets_values: ['disable', 0, 20, 3.5, 'euler', 'simple', 10, 10000, 'disable'],
    title: 'KSampler low (10-end)',
  });

  const dec = b.node('VAEDecode', {
    pos: [X(5), 40], size: [220, 60],
    inputs: [{ name: 'samples', type: 'LATENT' }, { name: 'vae', type: 'VAE' }],
    outputs: [{ name: 'IMAGE', type: 'IMAGE' }],
  });

  // MMAudio
  const mmModel = b.node('MMAudioModelLoader', {
    pos: [X(5), 160], size: [340, 90],
    outputs: [{ name: 'mmaudio_model', type: 'MMAUDIO_MODEL' }],
    widgets_values: [MM_MAIN, 'fp16'],
  });
  const mmFeat = b.node('MMAudioFeatureUtilsLoader', {
    pos: [X(5), 280], size: [340, 170],
    inputs: [{ name: 'bigvgan_vocoder_model', type: 'VOCODER_MODEL', shape: 7 }],
    outputs: [{ name: 'mmaudio_featureutils', type: 'MMAUDIO_FEATUREUTILS' }],
    widgets_values: [MM_VAE, MM_SYNC, MM_CLIP, '44k', 'fp16'],
  });
  const mmSamp = b.node('MMAudioSampler', {
    pos: [X(6), 40], size: [380, 300],
    inputs: [
      { name: 'mmaudio_model', type: 'MMAUDIO_MODEL' },
      { name: 'feature_utils', type: 'MMAUDIO_FEATUREUTILS' },
      { name: 'images', type: 'IMAGE', shape: 7 },
    ],
    outputs: [{ name: 'audio', type: 'AUDIO' }],
    // duration,steps,cfg,seed,prompt,negative_prompt,mask_away_clip,force_offload
    widgets_values: [5.06, 25, 4.5, 0, 'footsteps, ambient room tone, natural sound', 'music, voice, speech', false, true],
    title: 'MMAudio (81f/16fps = 5.06s)',
  });
  const combine = b.node('VHS_VideoCombine', {
    pos: [X(7), 40], size: [420, 380],
    inputs: [
      { name: 'images', type: 'IMAGE' },
      { name: 'audio', type: 'AUDIO', shape: 7 },
      { name: 'meta_batch', type: 'VHS_BatchManager', shape: 7 },
      { name: 'vae', type: 'VAE', shape: 7 },
    ],
    outputs: [{ name: 'Filenames', type: 'VHS_FILENAMES' }],
    widgets_values: {
      frame_rate: 16, loop_count: 0, filename_prefix: 'Wan22_MMAudio',
      format: 'video/h264-mp4', pix_fmt: 'yuv420p', crf: 19,
      save_metadata: true, pingpong: false, save_output: true,
    },
    title: 'Combine video + audio -> mp4',
  });

  // wire
  b.connect(clip, 0, posText, 0);
  b.connect(clip, 0, negText, 0);
  b.connect(unetHi, 0, shiftHi, 0);
  b.connect(unetLo, 0, shiftLo, 0);
  b.connect(posText, 0, i2v, 0);
  b.connect(negText, 0, i2v, 1);
  b.connect(vae, 0, i2v, 2);
  b.connect(loadImg, 0, i2v, 4); // start_image
  // high sampler
  b.connect(shiftHi, 0, kHi, 0);
  b.connect(i2v, 0, kHi, 1);
  b.connect(i2v, 1, kHi, 2);
  b.connect(i2v, 2, kHi, 3);
  // low sampler (continues from high latent)
  b.connect(shiftLo, 0, kLo, 0);
  b.connect(i2v, 0, kLo, 1);
  b.connect(i2v, 1, kLo, 2);
  b.connect(kHi, 0, kLo, 3);
  // decode
  b.connect(kLo, 0, dec, 0);
  b.connect(vae, 0, dec, 1);
  // mmaudio
  b.connect(mmModel, 0, mmSamp, 0);
  b.connect(mmFeat, 0, mmSamp, 1);
  b.connect(dec, 0, mmSamp, 2);
  // combine
  b.connect(dec, 0, combine, 0);
  b.connect(mmSamp, 0, combine, 1);

  return b.dump();
}

// ==========================================================================
// Workflow 2: Add audio to an existing video
// ==========================================================================
function buildAddAudio() {
  const b = new Builder();
  const X = c => c * 380;

  const loadVid = b.node('VHS_LoadVideo', {
    pos: [X(0), 40], size: [360, 500],
    inputs: [
      { name: 'meta_batch', type: 'VHS_BatchManager', shape: 7 },
      { name: 'vae', type: 'VAE', shape: 7 },
    ],
    outputs: [
      { name: 'IMAGE', type: 'IMAGE' },
      { name: 'frame_count', type: 'INT' },
      { name: 'audio', type: 'AUDIO' },
      { name: 'video_info', type: 'VHS_VIDEOINFO' },
    ],
    // video,force_rate,custom_width,custom_height,frame_load_cap,skip_first_frames,select_every_nth
    widgets_values: {
      video: 'input.mp4', force_rate: 0, custom_width: 0, custom_height: 0,
      frame_load_cap: 0, skip_first_frames: 0, select_every_nth: 1,
    },
    title: '1. Load your video',
  });
  const info = b.node('VHS_VideoInfo', {
    pos: [X(1), 40], size: [280, 220],
    inputs: [{ name: 'video_info', type: 'VHS_VIDEOINFO' }],
    outputs: [
      { name: 'source_fps', type: 'FLOAT' },
      { name: 'source_frame_count', type: 'INT' },
      { name: 'source_duration', type: 'FLOAT' },
      { name: 'source_width', type: 'INT' },
      { name: 'source_height', type: 'INT' },
      { name: 'loaded_fps', type: 'FLOAT' },
      { name: 'loaded_frame_count', type: 'INT' },
      { name: 'loaded_duration', type: 'FLOAT' },
      { name: 'loaded_width', type: 'INT' },
      { name: 'loaded_height', type: 'INT' },
    ],
    widgets_values: {},
  });

  const mmModel = b.node('MMAudioModelLoader', {
    pos: [X(1), 300], size: [340, 90],
    outputs: [{ name: 'mmaudio_model', type: 'MMAUDIO_MODEL' }],
    widgets_values: [MM_MAIN, 'fp16'],
  });
  const mmFeat = b.node('MMAudioFeatureUtilsLoader', {
    pos: [X(1), 420], size: [340, 170],
    inputs: [{ name: 'bigvgan_vocoder_model', type: 'VOCODER_MODEL', shape: 7 }],
    outputs: [{ name: 'mmaudio_featureutils', type: 'MMAUDIO_FEATUREUTILS' }],
    widgets_values: [MM_VAE, MM_SYNC, MM_CLIP, '44k', 'fp16'],
  });

  const mmSamp = b.node('MMAudioSampler', {
    pos: [X(2), 40], size: [380, 320],
    inputs: [
      { name: 'mmaudio_model', type: 'MMAUDIO_MODEL' },
      { name: 'feature_utils', type: 'MMAUDIO_FEATUREUTILS' },
      { name: 'images', type: 'IMAGE', shape: 7 },
      { name: 'duration', type: 'FLOAT', widget: { name: 'duration' }, shape: 7 },
    ],
    outputs: [{ name: 'audio', type: 'AUDIO' }],
    widgets_values: [5.0, 25, 4.5, 0, 'match the on-screen action, natural sound effects', 'music, voice, speech', false, true],
    title: 'MMAudio (duration from video)',
  });
  // duration input is fed by link; keep widget value as fallback

  const preview = b.node('PreviewAudio', {
    pos: [X(3), 40], size: [320, 90],
    inputs: [{ name: 'audio', type: 'AUDIO' }],
    outputs: [],
    widgets_values: [null],
  });
  const combine = b.node('VHS_VideoCombine', {
    pos: [X(3), 180], size: [420, 380],
    inputs: [
      { name: 'images', type: 'IMAGE' },
      { name: 'audio', type: 'AUDIO', shape: 7 },
      { name: 'meta_batch', type: 'VHS_BatchManager', shape: 7 },
      { name: 'vae', type: 'VAE', shape: 7 },
      { name: 'frame_rate', type: 'FLOAT', widget: { name: 'frame_rate' }, shape: 7 },
    ],
    outputs: [{ name: 'Filenames', type: 'VHS_FILENAMES' }],
    widgets_values: {
      frame_rate: 16, loop_count: 0, filename_prefix: 'MMAudio_added',
      format: 'video/h264-mp4', pix_fmt: 'yuv420p', crf: 19,
      save_metadata: true, pingpong: false, save_output: true,
    },
    title: 'Re-mux video + new audio',
  });

  // wire
  b.connect(loadVid, 3, info, 0);        // video_info -> info
  b.connect(loadVid, 0, mmSamp, 2);      // IMAGE -> images
  b.connect(info, 7, mmSamp, 3);         // loaded_duration -> duration
  b.connect(mmModel, 0, mmSamp, 0);
  b.connect(mmFeat, 0, mmSamp, 1);
  b.connect(mmSamp, 0, preview, 0);
  b.connect(loadVid, 0, combine, 0);     // IMAGE -> images
  b.connect(mmSamp, 0, combine, 1);      // audio
  b.connect(info, 5, combine, 4);        // loaded_fps -> frame_rate

  return b.dump();
}

const outDir = process.argv[2] || '.';
const w1 = buildWan();
const w2 = buildAddAudio();
fs.writeFileSync(path.join(outDir, 'wan22-i2v-mmaudio.json'), JSON.stringify(w1, null, 2));
fs.writeFileSync(path.join(outDir, 'add-audio-to-video-mmaudio.json'), JSON.stringify(w2, null, 2));
console.log('wrote wan22-i2v-mmaudio.json (' + w1.nodes.length + ' nodes, ' + w1.links.length + ' links)');
console.log('wrote add-audio-to-video-mmaudio.json (' + w2.nodes.length + ' nodes, ' + w2.links.length + ' links)');
