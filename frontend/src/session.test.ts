import {describe,expect,it} from 'vitest';
import {mergeSessionView,reduceEvent} from './session';
import type {RunEvent,Session,Turn} from './types';
const session:Session={id:'run',epoch:1,game:{id:'game',home_team:'KC',away_team:'BAL',mode:'replay',start_time:'2024-09-05',status:'scheduled',label:'Opener',home_score:0,away_score:0,quarter:0,clock:'15:00'},mode:'replay',provider:'demo',status:'paused',speed:2,index:0,total:100,turns:[],events:[],feed:{status:'ok'},metrics:{}};
const event=(seq:number,epoch:number,type:string,data:Record<string,unknown>):RunEvent=>({seq,epoch,type,data,session_id:'run',timestamp:'2026-01-01'});
const turn={id:'turn',agent_id:'a',text:'A call.'} as Turn;
describe('reconnect and cancellation',()=>{
  it('does not duplicate turns or trace events on reconnect',()=>{const first=reduceEvent(session,event(1,1,'turn',{turn}));const replayed=reduceEvent(first,event(1,1,'turn',{turn}));expect(replayed.turns).toHaveLength(1);expect(replayed.events).toHaveLength(1);});
  it('discards responses from a cancelled epoch',()=>{const fresh=reduceEvent(session,event(2,2,'state',{status:'paused'}));expect(reduceEvent(fresh,event(3,1,'turn',{turn}))).toEqual(fresh);});
  it('resets snapshot and old conversation on restart',()=>{const old=reduceEvent(session,event(1,1,'turn',{turn}));const restarted=reduceEvent(old,event(2,2,'state',{status:'paused',index:0,turns:[],snapshot:null}));expect(restarted.turns).toHaveLength(0);expect(restarted.epoch).toBe(2);});
  it('retains the original transcript and traces on pause',()=>{const old=reduceEvent(session,event(1,1,'turn',{turn}));const paused=reduceEvent(old,event(2,2,'state',{status:'paused'}));expect(paused.turns).toHaveLength(1);expect(paused.events).toHaveLength(2);});
  it('ignores events belonging to another run',()=>{expect(reduceEvent(session,{...event(1,1,'turn',{turn}),session_id:'other'})).toBe(session);});
  it('does not overwrite streamed results with a delayed control response',()=>{const streamed=reduceEvent(session,event(9,1,'turn',{turn}));expect(mergeSessionView(streamed,{...session,seq:1,status:'stepping'})).toBe(streamed);});
  it('accepts a newer epoch even when the previous view has a higher cursor',()=>{const old={...session,seq:99};expect(mergeSessionView(old,{...session,seq:100,epoch:2}).epoch).toBe(2);});
});
