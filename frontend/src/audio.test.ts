import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {BroadcastAudio,decodePCM} from './audio';
const pcm=(samples:number[])=>btoa(String.fromCharCode(...samples.flatMap(v=>[v&255,(v>>8)&255])));
const scheduled:number[]=[];
const stopped:ReturnType<typeof vi.fn>[]=[];
class Context{
  currentTime=0;destination={};
  async resume(){}
  createBuffer(_channels:number,length:number,rate:number){return {duration:length/rate,copyToChannel:vi.fn()};}
  createBufferSource(){const stop=vi.fn();stopped.push(stop);return {buffer:null as {duration:number}|null,connect:vi.fn(),stop,start:(when:number)=>scheduled.push(when)};}
}
beforeEach(()=>{vi.useFakeTimers();scheduled.length=0;stopped.length=0;vi.stubGlobal('AudioContext',Context);vi.stubGlobal('window',{speechSynthesis:{cancel:vi.fn(),getVoices:()=>[],speak:vi.fn()}});});
afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals();});
describe('broadcast audio ownership',()=>{
  it('decodes signed little endian PCM16',()=>{expect([...decodePCM(pcm([0,32767,-32768]))]).toEqual([0,32767/32768,-1]);});
  it('plays chunks in index order and acknowledges after the final sample',async()=>{const ack=vi.fn(),speaking=vi.fn(),audio=new BroadcastAudio(speaking,ack);await audio.unlock();audio.chunk({segment_id:'one',agent_id:'a',chunk_index:1,audio:pcm([1,1])});expect(scheduled).toHaveLength(0);audio.chunk({segment_id:'one',agent_id:'a',chunk_index:0,audio:pcm([1,1])});audio.seal({segment_id:'one',total_samples:4});expect(scheduled).toHaveLength(2);expect(scheduled[1]).toBeGreaterThan(scheduled[0]);expect(ack).not.toHaveBeenCalled();await vi.runAllTimersAsync();expect(ack).toHaveBeenCalledWith('one',4,false);});
  it('buffers a peer until the lead has finished',async()=>{const ack=vi.fn(),audio=new BroadcastAudio(vi.fn(),ack);await audio.unlock();audio.chunk({segment_id:'lead',agent_id:'a',chunk_index:0,audio:pcm([1,1])});audio.chunk({segment_id:'peer',agent_id:'b',chunk_index:0,audio:pcm([1,1])});expect(scheduled).toHaveLength(1);audio.seal({segment_id:'lead',total_samples:2});audio.seal({segment_id:'peer',total_samples:2});await vi.runAllTimersAsync();expect(scheduled).toHaveLength(2);expect(ack.mock.calls.map(call=>call[0])).toEqual(['lead','peer']);});
  it('muting cancels pending audio and never completes it later',async()=>{const ack=vi.fn(),audio=new BroadcastAudio(vi.fn(),ack);await audio.unlock();audio.chunk({segment_id:'lead',agent_id:'a',chunk_index:0,audio:pcm([1,1])});audio.seal({segment_id:'lead',total_samples:2});audio.mute(true);await vi.runAllTimersAsync();expect(ack).toHaveBeenCalledTimes(1);expect(ack).toHaveBeenCalledWith('lead',2,true);});
  it('muted browser speech is immediately acknowledged without autoplay',()=>{const ack=vi.fn(),audio=new BroadcastAudio(vi.fn(),ack);audio.demo('A real play.','a','demo');expect(ack).toHaveBeenCalledWith('demo',0,true);expect(window.speechSynthesis.speak).not.toHaveBeenCalled();});
  it('discards failed lead audio and safely starts its buffered peer',async()=>{
    const ack=vi.fn(),audio=new BroadcastAudio(vi.fn(),ack);await audio.unlock();
    audio.chunk({segment_id:'failed',agent_id:'a',chunk_index:0,audio:pcm([1,1])});
    audio.seal({segment_id:'failed',total_samples:2});
    audio.chunk({segment_id:'peer',agent_id:'b',chunk_index:0,audio:pcm([1,1])});
    audio.seal({segment_id:'peer',total_samples:2});
    audio.seal({segment_id:'failed',total_samples:2,discard_audio:true});
    expect(stopped[0]).toHaveBeenCalledTimes(1);expect(stopped[1]).not.toHaveBeenCalled();
    expect(scheduled).toHaveLength(2);
    await vi.runAllTimersAsync();
    expect(ack.mock.calls).toEqual([['failed',2,true],['peer',2,false]]);
    audio.chunk({segment_id:'failed',agent_id:'a',chunk_index:1,audio:pcm([1,1])});
    audio.seal({segment_id:'failed',total_samples:4});
    expect(scheduled).toHaveLength(2);expect(ack).toHaveBeenCalledTimes(2);
  });
  it('discards a buffered peer without stopping the current speaker',async()=>{
    const ack=vi.fn(),audio=new BroadcastAudio(vi.fn(),ack);await audio.unlock();
    audio.chunk({segment_id:'lead',agent_id:'a',chunk_index:0,audio:pcm([1,1])});
    audio.seal({segment_id:'lead',total_samples:2});
    audio.chunk({segment_id:'failed-peer',agent_id:'b',chunk_index:0,audio:pcm([1,1])});
    audio.seal({segment_id:'failed-peer',total_samples:2,discard_audio:true});
    expect(stopped[0]).not.toHaveBeenCalled();expect(scheduled).toHaveLength(1);
    await vi.runAllTimersAsync();
    expect(ack.mock.calls).toEqual([['failed-peer',0,true],['lead',2,false]]);
  });
  it('acknowledges a muted failed segment and rejects its late chunks after unmute',async()=>{
    const ack=vi.fn(),audio=new BroadcastAudio(vi.fn(),ack);
    audio.chunk({segment_id:'failed',agent_id:'a',chunk_index:0,audio:pcm([1,1])});
    audio.seal({segment_id:'failed',total_samples:2,discard_audio:true});
    await audio.unlock();audio.chunk({segment_id:'failed',agent_id:'a',chunk_index:1,audio:pcm([1,1])});
    expect(scheduled).toHaveLength(0);expect(ack).toHaveBeenCalledExactlyOnceWith('failed',0,true);
  });
});
