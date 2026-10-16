import { LIVE_LAYOUTS, eventPlacement, hitEvent, routeTarget, livePose, alpha } from './live-layout.js';
import { legPose, heartbeatAt, drawSpiderBody } from './spider.js';
import { probePose } from './probes.js';

const ROUTES=['code','model','you'];
const STATUS={pending:'等待',running:'进行中',completed:'已完成',failed:'失败'};
const MONO='Consolas, "Cascadia Code", monospace';
export class LiveRenderer {
  constructor(canvas,{onSelect}={}) {
    this.canvas=canvas;this.ctx=canvas.getContext('2d',{alpha:false});this.regions=[];
    this.handleClick=event=>{const id=this.hitTest(event.clientX,event.clientY);if(id!==null)onSelect?.(id);};
    canvas.addEventListener('click',this.handleClick);
  }
  destroy(){this.canvas.removeEventListener('click',this.handleClick);}
  hitTest(clientX,clientY){const r=this.canvas.getBoundingClientRect();return hitEvent(this.regions,(clientX-r.left)/r.width*this.layout.width,(clientY-r.top)/r.height*this.layout.height);}
  resize(layout){
    const dpr=Math.min(2,Math.max(1,globalThis.devicePixelRatio||1));
    if(this.layout===layout && this.dpr===dpr && this.canvas.width===Math.round(layout.width*dpr) && this.canvas.height===Math.round(layout.height*dpr))return;
    this.layout=layout;this.dpr=dpr;this.regions=[];
    this.canvas.width=Math.round(layout.width*dpr);this.canvas.height=Math.round(layout.height*dpr);
    this.canvas.style.aspectRatio=`${layout.width} / ${layout.height}`;
    this.ctx.setTransform(dpr,0,0,dpr,0,0);
  }
  text(value,x,y,color,size=12,align='left',bold=false){const c=this.ctx;c.font=`${bold?700:400} ${size}px ${MONO}`;c.fillStyle=color;c.textAlign=align;c.textBaseline='alphabetic';c.fillText(String(value),x,y);c.textAlign='left';}
  label(value,maxWidth){const c=this.ctx;let text=String(value);while(text.length>1 && c.measureText(text).width>maxWidth)text=text.slice(0,-1);return text===String(value)?text:text.slice(0,-1)+'…';}
  line(x1,y1,x2,y2,color,width=1){const c=this.ctx;c.beginPath();c.moveTo(x1,y1);c.lineTo(x2,y2);c.strokeStyle=color;c.lineWidth=width;c.stroke();}
  panel(rect,title,theme,right='') {
    const c=this.ctx,{x,y,w,h}=rect;c.fillStyle=theme.panel;c.fillRect(x,y,w,h);c.strokeStyle=theme.border;c.lineWidth=1;c.strokeRect(x+.5,y+.5,w-1,h-1);
    c.fillStyle=theme.panelTop;c.fillRect(x+1,y+1,w-2,30);this.text(title,x+13,y+20,theme.muted,11);this.text(right,x+w-12,y+20,theme.muted,10,'right');
  }
  render(view,{time=view.at,reducedMotion=false,selectedId=null,page=0,routeFilter='all',connection='离线回放'}={}){
    const {config}=view,theme=config.theme,layout=LIVE_LAYOUTS[config.layout]||LIVE_LAYOUTS.portrait;this.resize(layout);
    const c=this.ctx;c.fillStyle=theme.background;c.fillRect(0,0,layout.width,layout.height);
    this.text(config.title,40,53,theme.text,27,'left',true);this.text(config.subtitle||'EVERY TASK, ONE FORK',40,79,theme.accent,12);
    this.text(connection,layout.width-40,48,theme.muted,10,'right');this.text(config.modelName,layout.width-40,76,config.routes.model.color,12,'right');
    const scene=layout.scene;this.panel(scene,config.unit==='word'?'PROMPT / TOKEN STREAM':'AGENT / TASK STREAM',theme,`PAGE ${page+1} · ${view.totalEvents} EVENTS`);
    c.save();c.beginPath();c.rect(scene.x+1,scene.y+31,scene.w-2,scene.h-32);c.clip();
    this.regions=eventPlacement(layout,view.events).filter(x=>routeFilter==='all'||x.event.route===routeFilter);
    this.web(view,time,reducedMotion,selectedId);
    for(const region of this.regions){
      const {event,x,y,w,h}=region,color=config.routes[event.route].color,selected=event.id===selectedId;
      if(selected||event.status==='running'){c.fillStyle=alpha(selected?theme.accent:color,.13);c.fillRect(x-6,y-h/2,w+8,h);}
      this.text(String(event.order??region.index+1).padStart(3,'0'),x,y+4,theme.muted,9);
      this.text(this.label(event.displayLabel??event.label,w-64),x+35,y+4,color,11);
      c.fillStyle=event.status==='failed'?theme.accent:color;c.globalAlpha=event.status==='pending'?.35:1;c.fillRect(x+w-12,y-3,5,5);c.globalAlpha=1;
    }
    if(!view.totalEvents){this.text('等待 Agent 上报第一个事件',scene.x+30,scene.y+93,theme.text,16);this.text('每一次真实步骤，都会在这里结成一张网。',scene.x+30,scene.y+121,theme.muted,11);}
    c.restore();
    if(layout.stream)this.stream(view,layout.stream,theme);
    this.stats(view,layout.stats,theme);
    this.text(`${config.unit.toUpperCase()} MODE   /   ${view.status.toUpperCase()}   /   ${view.at.toFixed(2)} s`,40,layout.height-18,theme.muted,10);
    this.text('真实事件 · 由 Agent 主动上报',layout.width-40,layout.height-18,theme.accent,10,'right');
  }
  web(view,time,reducedMotion,selectedId){
    const c=this.ctx,{config}=view,t=config.theme,s=this.layout.scene,body=livePose(this.layout,time,reducedMotion);
    // 底色：一次线性渐变铺出网的纵深感（只建一次，不每帧造对象）
    const wash=c.createLinearGradient(s.x+s.w*.3,0,s.x+s.w,0);
    wash.addColorStop(0,alpha(t.spider,0));wash.addColorStop(.55,alpha(t.spider,.028));wash.addColorStop(1,alpha(t.spider,0));
    c.fillStyle=wash;c.fillRect(s.x+s.w*.3,s.y+31,s.w*.7,s.h-32);

    // 网丝：锚点带轻微呼吸，让静态网格也有生命感
    const breathe=reducedMotion?0:Math.sin(time*.7)*4;
    for(let i=0;i<24;i++){
      const y=s.y+55+(s.h-85)*i/23,target=routeTarget(this.layout,ROUTES[i%3]);
      const sag=(i%2?6:-6)+breathe*(i%2?1:-1)*.5;
      c.beginPath();c.moveTo(s.x+s.w*.33,y);
      c.bezierCurveTo(s.x+s.w*.56,y+sag,s.x+s.w*.65,target.y-sag*.4,target.x,target.y);
      c.strokeStyle=alpha(t.spider,.055);c.lineWidth=.65;c.stroke();
    }
    for(const region of this.regions){const event=region.event,color=config.routes[event.route].color,target=routeTarget(this.layout,event.route);
      const sel=event.id===selectedId;
      // 双线：外圈柔光 + 内芯实线，取代原来的单根 0.7px
      const midX=s.x+s.w*.52,midY=region.y,tX=s.x+s.w*.66,tY=target.y;
      if(sel){c.beginPath();c.moveTo(region.x+region.w,region.y);c.bezierCurveTo(midX,midY,tX,tY,target.x,target.y);
        c.strokeStyle=alpha(t.accent,.20);c.lineWidth=4.5;c.stroke();}
      c.beginPath();c.moveTo(region.x+region.w,region.y);c.bezierCurveTo(midX,midY,tX,tY,target.x,target.y);
      c.strokeStyle=alpha(sel?t.accent:color,sel?.92:.24);c.lineWidth=sel?1.4:.72;c.stroke();
    }
    const active=new Map(view.activeEvents.slice(-200).map(event=>[event.id,event]));
    for(const region of this.regions){const event=active.get(region.event.id);if(!event)continue;const age=time-event.firstAt;if(age<0||age>3.8)continue;
      const color=config.routes[event.route].color,target=routeTarget(this.layout,event.route),from={x:region.x+region.w/2,y:region.y};
      const pose=probePose({index:event.order,word:event.label,target:from,launchTime:event.firstAt+.26},time,body,reducedMotion);
      if(pose.visible){c.globalAlpha=pose.opacity;c.beginPath();c.moveTo(pose.root.x,pose.root.y);c.bezierCurveTo(pose.joints[0].x,pose.joints[0].y,pose.joints[1].x,pose.joints[1].y,pose.tip.x,pose.tip.y);c.setLineDash([1.8,4]);c.strokeStyle=t.spider;c.lineWidth=1.2;c.stroke();c.setLineDash([]);c.globalAlpha=1;}
      if(age>=.52){const p=reducedMotion?1:Math.min(1,(age-.52)/1.2),q=1-p;const x=q*q*from.x+2*q*p*(s.x+s.w*.65)+p*p*target.x,y=q*q*from.y+2*q*p*from.y+p*p*target.y;
        // 飞行的词条：外光晕 + 实心菱核
        c.fillStyle=alpha(color,.22);c.beginPath();c.arc(x,y,7-p*3,0,6.2832);c.fill();
        c.fillStyle=color;c.fillRect(x-3,y-3,6,6);
        c.fillStyle=alpha('#ffffff',.55);c.fillRect(x-1,y-1,2,2);
        if(p<1)this.text(this.label(event.label,120),x+9,y+4,color,10);}
    }
    this.spider(body,time,reducedMotion,t);
    for(const route of ROUTES){const p=routeTarget(this.layout,route),color=config.routes[route].color,w=s.w*.19,h=100;
      // 分支卡：顶部渐变条 + 内描边 + 计数辉光
      c.fillStyle=t.panel;c.fillRect(p.x-w/2,p.y-h/2,w,h);
      const cap=c.createLinearGradient(p.x-w/2,0,p.x+w/2,0);cap.addColorStop(0,alpha(color,.95));cap.addColorStop(1,alpha(color,.35));
      c.fillStyle=cap;c.fillRect(p.x-w/2,p.y-h/2,w,5);
      c.strokeStyle=alpha(color,.85);c.lineWidth=1;c.strokeRect(p.x-w/2+.5,p.y-h/2+.5,w-1,h-1);
      c.strokeStyle=alpha(color,.14);c.strokeRect(p.x-w/2+3.5,p.y-h/2+3.5,w-7,h-7);
      const n=view.counts[route]??0;
      if(n>0){c.save();c.shadowColor=color;c.shadowBlur=14;this.text(n,p.x+w/2-12,p.y+18,color,29,'right',true);c.restore();}
      else this.text(n,p.x+w/2-12,p.y+18,alpha(color,.5),29,'right',true);
      this.text(config.routes[route].label,p.x-w/2+12,p.y-20,color,12);this.text(route.toUpperCase(),p.x-w/2+12,p.y+37,t.muted,9);}
  }
  spider(body,time,reducedMotion,theme){
    // 绘制逻辑已抽到 spider.js 的 drawSpiderBody，实时面板与美术查看器共用同一份，
    // 避免两处画风分叉。
    drawSpiderBody(this.ctx,body,time,reducedMotion,theme);
  }
  stream(view,rect,t){this.panel(rect,'RECENT / EVENT DETAILS',t,'CLICK TASK TO INSPECT');const rows=view.events.slice(-Math.floor((rect.h-45)/29));for(let i=0;i<rows.length;i++){const e=rows[i],y=rect.y+55+i*29,color=view.config.routes[e.route].color;this.text(this.label(e.label,rect.w*.52),rect.x+13,y,t.text,11);this.text(STATUS[e.status],rect.x+rect.w*.60,y,color,10);this.text(e.score===null||e.score===undefined?'分数未提供':Number(e.score).toFixed(3),rect.x+rect.w-13,y,t.muted,10,'right');}}
  stats(view,rect,t){
    this.panel(rect,'SESSION / REPORTED METRICS',t);const compact=rect.h<180;
    if(compact){const items=[['已上报',view.totalEvents],['完成',view.statusCounts.completed??0],['失败',view.statusCounts.failed??0],['预计总量',view.config.expectedTotal??'未提供'],['模型调用','未提供']];items.forEach(([label,value],i)=>{const x=rect.x+20+i*(rect.w-40)/items.length;this.text(label,x,rect.y+64,t.muted,11);this.text(value,x,rect.y+100,i===0?t.accent:t.text,22);});return;}
    this.text(view.totalEvents,rect.x+20,rect.y+83,t.accent,43,'left',true);this.text('真实上报事件',rect.x+20,rect.y+106,t.muted,11);
    let y=rect.y+141;for(const route of ROUTES){const color=view.config.routes[route].color,label=view.config.routes[route].label;this.text(this.label(label,100),rect.x+20,y,color,11);cFill(this.ctx,rect.x+125,y-8,Math.max(1,(rect.w-175)*(view.counts[route]??0)/Math.max(1,view.totalEvents)),5,color);this.text(view.counts[route]??0,rect.x+rect.w-15,y,color,11,'right');y+=28;}
    this.text(`预计总量 ${view.config.expectedTotal??'未提供'}`,rect.x+20,y+9,t.muted,11);this.text('模型调用 未提供',rect.x+20,y+32,t.muted,11);
  }
}
function cFill(ctx,x,y,w,h,color){ctx.fillStyle=color;ctx.fillRect(x,y,w,h);}
