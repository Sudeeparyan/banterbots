type Chunk = {segment_id:string;agent_id:string;chunk_index:number;audio:string;rate?:number};
type Seal = {segment_id:string;total_samples:number;agent_id?:string;discard_audio?:boolean};
type Segment = {id:string;agent:string;chunks:Map<number,Float32Array>;next:number;sealed:boolean;total:number;played:number;scheduled:number;sources:AudioBufferSourceNode[]};
export function decodePCM(base64:string):Float32Array{
  const bytes=Uint8Array.from(atob(base64),c=>c.charCodeAt(0));
  const view=new DataView(bytes.buffer);const samples=new Float32Array(Math.floor(bytes.length/2));
  for(let i=0;i<samples.length;i++)samples[i]=view.getInt16(i*2,true)/32768;
  return samples;
}
export class BroadcastAudio {
  private context:AudioContext|null=null;
  private segments:Segment[]=[];
  private muted=true;
  private timer:ReturnType<typeof setTimeout>|undefined;
  private generation=0;
  private speakQueue:{text:string;agent:string;id:string}[]=[];
  private speakingDemo=false;
  private activeDemo:string|null=null;
  private pendingAck:Set<string>=new Set();
  private discarded:Set<string>=new Set();
  constructor(private speaking:(agent:string|null)=>void,private ack:(id:string,samples:number,cancelled:boolean)=>void){}
  async unlock(){this.muted=false;this.context??=new AudioContext({sampleRate:24000});await this.context.resume();}
  mute(value:boolean){this.muted=value;if(value)this.clear();}
  clear(){
    this.generation++;if(this.timer)clearTimeout(this.timer);
    this.segments.forEach(s=>{s.sources.forEach(src=>{try{src.stop();}catch{/* source finished */}});this.ack(s.id,s.played,true);});
    if(this.activeDemo)this.ack(this.activeDemo,0,true);this.activeDemo=null;
    this.speakQueue.forEach(s=>this.ack(s.id,0,true));
    this.pendingAck.forEach(id=>this.ack(id,0,true));this.pendingAck.clear();this.segments=[];this.speakQueue=[];this.speakingDemo=false;
    if('speechSynthesis' in window)window.speechSynthesis.cancel();this.speaking(null);
  }
  chunk(chunk:Chunk){
    if(this.discarded.has(chunk.segment_id))return;
    if(this.muted){this.pendingAck.add(chunk.segment_id);return;}
    let segment=this.segments.find(s=>s.id===chunk.segment_id);
    if(!segment){segment={id:chunk.segment_id,agent:chunk.agent_id,chunks:new Map(),next:0,sealed:false,total:0,played:0,scheduled:0,sources:[]};this.segments.push(segment);}
    if(chunk.chunk_index>=segment.next&&!segment.chunks.has(chunk.chunk_index))segment.chunks.set(chunk.chunk_index,decodePCM(chunk.audio));
    this.pump();
  }
  seal(seal:Seal){
    if(this.discarded.has(seal.segment_id))return;
    if(seal.discard_audio){this.discard(seal.segment_id);return;}
    if(this.muted||this.pendingAck.has(seal.segment_id)){this.ack(seal.segment_id,0,true);this.pendingAck.delete(seal.segment_id);return;}
    let segment=this.segments.find(s=>s.id===seal.segment_id);
    if(!segment){segment={id:seal.segment_id,agent:seal.agent_id??'a',chunks:new Map(),next:0,sealed:true,total:seal.total_samples,played:0,scheduled:0,sources:[]};this.segments.push(segment);}
    segment.sealed=true;segment.total=seal.total_samples;this.pump();
  }
  private discard(id:string){
    this.discarded.add(id);
    // Keep a bounded tombstone set so late chunks cannot resurrect failed speech.
    if(this.discarded.size>1024)this.discarded.delete(this.discarded.values().next().value!);
    this.pendingAck.delete(id);
    const index=this.segments.findIndex(segment=>segment.id===id);
    if(index<0){this.ack(id,0,true);return;}
    const [segment]=this.segments.splice(index,1);
    segment.sources.forEach(source=>{try{source.stop();}catch{/* source already finished */}});
    if(index===0){if(this.timer)clearTimeout(this.timer);this.timer=undefined;}
    this.ack(id,segment.played,true);
    if(index===0){this.speaking(null);this.pump();}
  }
  private pump(){
    const context=this.context,segment=this.segments[0];if(!context||!segment||this.muted)return;
    this.speaking(segment.agent);
    while(segment.chunks.has(segment.next)){
      const pcm=segment.chunks.get(segment.next)!;segment.chunks.delete(segment.next++);
      const buffer=context.createBuffer(1,pcm.length,24000);buffer.copyToChannel(pcm as Float32Array<ArrayBuffer>,0);
      const source=context.createBufferSource();source.buffer=buffer;source.connect(context.destination);
      const when=Math.max(context.currentTime+0.015,segment.scheduled);segment.scheduled=when+buffer.duration;
      segment.played+=pcm.length;segment.sources.push(source);source.start(when);
    }
    if(segment.sealed&&segment.played>=segment.total){
      if(this.timer)clearTimeout(this.timer);const generation=this.generation;
      this.timer=setTimeout(()=>{if(generation!==this.generation||this.segments[0]!==segment)return;this.timer=undefined;this.ack(segment.id,segment.played,false);this.segments.shift();this.speaking(null);this.pump();},Math.max(0,(segment.scheduled-context.currentTime)*1000)+15);
    }
  }
  demo(text:string,agent:string,id:string){if(this.muted||!('speechSynthesis' in window)){this.ack(id,0,true);return;}this.speakQueue.push({text,agent,id});this.pumpDemo();}
  private pumpDemo(){
    if(this.speakingDemo||this.muted)return;const item=this.speakQueue.shift();if(!item)return;
    this.speakingDemo=true;this.activeDemo=item.id;const generation=this.generation;const utterance=new SpeechSynthesisUtterance(item.text);
    const voices=window.speechSynthesis.getVoices().filter(v=>v.lang.startsWith('en'));utterance.voice=voices[item.agent==='a'?0:Math.min(1,voices.length-1)]??null;
    utterance.rate=item.agent==='a'?1.09:0.98;utterance.pitch=item.agent==='a'?1.03:0.86;
    const done=()=>{if(generation!==this.generation)return;this.ack(item.id,0,false);this.activeDemo=null;this.speakingDemo=false;this.speaking(null);this.pumpDemo();};
    utterance.onstart=()=>this.speaking(item.agent);utterance.onend=done;utterance.onerror=done;window.speechSynthesis.speak(utterance);
  }
}
