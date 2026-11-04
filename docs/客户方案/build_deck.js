const pptxgen = require('pptxgenjs');
const path = require('path');

const pptx = new pptxgen();
pptx.layout = 'LAYOUT_WIDE';
pptx.author = 'spider-gd';
pptx.subject = '广东电力交易数据采集系统客户方案';
pptx.title = '广东电力交易数据采集解决方案';
pptx.lang = 'zh-CN';
pptx.theme = {
  headFontFace: 'Arial Unicode MS', bodyFontFace: 'Arial Unicode MS', lang: 'zh-CN'
};

const C = { navy:'112B3C', teal:'007E83', mint:'D9EFEB', ice:'F2F7F7', ink:'203642', grey:'5C6D75', line:'D5E1E3', white:'FFFFFF', amber:'B76C24' };
const SW = 13.333, SH = 7.5;
function txt(s,text,x,y,w,h,size=20,color=C.ink,bold=false,opts={}) {
  s.addText(text,{x,y,w,h,fontFace:'Arial Unicode MS',fontSize:size,color,bold,margin:0,
    breakLine:false,fit:'shrink',valign:'mid',...opts});
}
function bg(s,color=C.white){s.background={color};}
function line(s,x1,y1,x2,y2,color=C.line,width=1){s.addShape(pptx.ShapeType.line,{x:x1,y:y1,w:x2-x1,h:y2-y1,line:{color,width}});}
function rect(s,x,y,w,h,fill=C.ice,stroke=null,r=0){s.addShape(r?pptx.ShapeType.roundRect:pptx.ShapeType.rect,{x,y,w,h,rectRadius:r,fill:{color:fill},line:stroke?{color:stroke,width:1}:{color:fill,transparency:100},radius:r});}
function header(s,n,title,sub){
  txt(s,String(n).padStart(2,'0'),.58,.36,.48,.3,12,C.teal,true);
  txt(s,title,.58,.77,12.1,.64,29,C.navy,true);
  if(sub) txt(s,sub,.6,1.43,12,.43,14,C.grey,false);
  line(s,.58,1.97,12.73,1.97,C.line,1);
  txt(s,'广东电力交易数据采集解决方案',.58,7.12,6,.18,9,C.grey);
  txt(s,String(n).padStart(2,'0'),12.35,7.12,.38,.18,9,C.grey,false,{align:'right'});
}
function bullet(s,head,body,x,y,w){
  rect(s,x,y,.08,.72,C.teal);
  txt(s,head,x+.25,y-.01,w-.25,.30,17,C.navy,true);
  txt(s,body,x+.25,y+.34,w-.25,.40,13,C.grey,false,{valign:'top'});
}
function row(s,label,value,y,alt=false){
  if(alt) rect(s,.6,y,12.13,.7,C.ice);
  txt(s,label,.85,y+.12,2.45,.43,17,C.navy,true);
  txt(s,value,3.3,y+.10,9.05,.47,16,C.ink);
  line(s,.6,y+.7,12.73,y+.7,C.line,.6);
}

// 1 — 封面
{
  let s=pptx.addSlide(); bg(s,C.navy);
  rect(s,.66,.78,.13,5.72,C.teal);
  txt(s,'广东电力交易数据',1.12,1.34,10.5,.62,36,C.white,true);
  txt(s,'智能采集解决方案',1.12,2.05,10.5,.77,44,C.white,true);
  txt(s,'以现场专网接入为前提，形成采集、留存、监控与异常处置闭环',1.15,3.26,10.4,.7,20,'CFE3E4');
  line(s,1.15,5.67,11.93,5.67,'5E8190',1);
  txt(s,'客户沟通版  ·  2026年10月',1.15,5.88,6.3,.37,15,'B8D2D5');
  txt(s,'spider-gd',10.05,5.88,1.85,.37,15,'B8D2D5',true,{align:'right'});
}

// 2 — 业务问题
{
  let s=pptx.addSlide(); bg(s); header(s,2,'从人工下载到可追溯的日常采集','面向电力交易平台的数据获取与现场运维');
  bullet(s,'采集任务重复','每天进入平台、选择日期和指标、下载并整理文件。',.77,2.34,5.65);
  bullet(s,'专网环境特殊','U-Key 接入交易专网，网络与证书状态会影响登录。',6.78,2.34,5.6);
  bullet(s,'失败容易被漏看','验证码、证书弹窗或导出失败需要明确停止和告警。',.77,3.63,5.65);
  bullet(s,'历史文件难追踪','同名文件反复下载，需判断内容变化并保留旧版。',6.78,3.63,5.6);
  rect(s,.72,5.44,11.87,.82,C.mint);
  txt(s,'目标：让日常采集按计划运行，让异常及时暴露，让每份交付文件有迹可查。',1.04,5.60,11.25,.48,19,C.navy,true);
}

// 3 — 方案运行闭环
{
  let s=pptx.addSlide(); bg(s); header(s,3,'一条可查看、可干预的采集闭环','桌面端统一配置任务，采集结果落到客户本机');
  const steps=[
    ['01','配置计划','选择工作流、时间与目标日期'],
    ['02','接入登录','通过 U-Key 专网进入平台'],
    ['03','执行采集','按页面流程抓取并导出数据'],
    ['04','本地留存','保存文件，记录内容版本'],
    ['05','查看结果','在日历、日志和文件中心核查']
  ];
  steps.forEach((a,i)=>{
    let x=.7+i*2.52;
    txt(s,a[0],x,2.42,1.1,.55,26,C.teal,true);
    line(s,x,3.08,x+2.15,3.08,C.teal,2);
    txt(s,a[1],x,3.34,2.15,.46,18,C.navy,true);
    txt(s,a[2],x,3.94,2.15,1.18,15,C.grey,false,{valign:'top'});
  });
  rect(s,.72,5.72,11.88,.6,C.ice);
  txt(s,'异常在当前步骤显示失败原因，必要时暂停整次任务并发送通知。',.98,5.80,11.25,.43,16,C.navy,true);
}

// 4 — 数据范围
{
  let s=pptx.addSlide(); bg(s); header(s,4,'覆盖主要交易与披露数据','工作流已配置，具体指标按客户账号权限和现场页面验收');
  row(s,'现货交易','分日出清量价、分时分类型出清电量、节点电价',2.28,true);
  row(s,'信息披露','实际信息、预测信息、发电企业数据',2.98,false);
  row(s,'结算管理','日清算概况、日清算概览',3.68,true);
  row(s,'交易行情','地区分布、全省发电侧加权价格曲线',4.38,false);
  row(s,'中长期交易','月份信息明细',5.08,true);
  txt(s,'已对登录、日前/实时节点电价、日清算概览、现货完整采集等 5 个参考工作流完成 mocksite 端到端回归。',.82,6.12,11.75,.48,13,C.grey);
}

// 5 — 异常与质量
{
  let s=pptx.addSlide(); bg(s); header(s,5,'关键失败会停下来，并留下证据','避免登录失败后继续重试或把失败任务误报为完成');
  const yy=[2.32,3.25,4.18,5.11];
  const entries=[
    ['登录异常','证书/PIN 弹窗、敏感变量未替换、登录后未进入工作台时暂停，终止本次自动重试。'],
    ['网络异常','站点不可达时标记失败，提供专网、路由、DNS 等诊断线索。'],
    ['导出异常','下载失败可尝试工作流配置的接口兜底；仍失败则阻断完成状态。'],
    ['通知与留证','失败原因进入执行日志；验证码失败留截图，通知失败写本地队列待补发。']
  ];
  entries.forEach((e,i)=>{
    rect(s,.73,yy[i],.12,.64,i===0?C.amber:C.teal);
    txt(s,e[0],1.07,yy[i]-.04,2.12,.5,18,C.navy,true);
    txt(s,e[1],3.06,yy[i]-.03,9.31,.65,16,C.ink);
    if(i<3) line(s,.73,yy[i]+.75,12.55,yy[i]+.75,C.line,.7);
  });
}

// 6 — 数据资产
{
  let s=pptx.addSlide(); bg(s); header(s,6,'文件交付与运行记录可以直接核查','数据留在本地目录；文件中心呈现当前版本和历史版本');
  bullet(s,'同内容不重复覆盖','系统按 SHA256 比较文件内容。',.78,2.38,5.7);
  bullet(s,'变化时保留旧版','新内容占当前路径，旧文件归档到 _versions。',6.72,2.38,5.6);
  bullet(s,'文件中心查看','打开下载目录、压缩文件，查看版本摘要和归档时间。',.78,3.72,5.7);
  bullet(s,'日志用于排障','按任务查看步骤与失败原因，支持上传日志和诊断包。',6.72,3.72,5.6);
  rect(s,.72,5.43,11.88,.84,C.mint);
  txt(s,'文件、执行记录、诊断信息形成同一条可追溯链路。',1.03,5.57,11.15,.49,20,C.navy,true);
}

// 7 — 部署
{
  let s=pptx.addSlide(); bg(s); header(s,7,'一台 Windows 采集机即可形成现场方案','外网能力用于远程通知与上传；专网采集可在离线模式持续落地');
  const cards=[
    {x:.76,w:3.48,t:'交易专网',b:'U-Key 随身 WiFi\n连接电力交易平台'},
    {x:4.87,w:3.48,t:'采集机',b:'桌面管理端 + 采集服务\n定时执行、文件落地、日志'},
    {x:8.98,w:3.48,t:'可选外网',b:'有线或独立网卡\n用于通知、上传与远程支持'}
  ];
  cards.forEach(c=>{rect(s,c.x,2.58,c.w,2.0,C.ice,C.line,.10);txt(s,c.t,c.x+.25,2.86,c.w-.5,.5,22,C.navy,true,{align:'center'});txt(s,c.b,c.x+.28,3.48,c.w-.56,.72,16,C.grey,false,{align:'center',breakLine:false});});
  line(s,4.24,3.55,4.78,3.55,C.teal,2);
  line(s,8.36,3.55,8.89,3.55,C.teal,2);
  txt(s,'现场前置条件',.83,5.14,2.4,.4,18,C.teal,true);
  txt(s,'Windows 主机、有效 U-Key 与账号、目标平台访问权限、数据目录；联网通知需另有外网出口和通道配置。',.83,5.68,11.65,.72,17,C.ink);
}

// 8 — 操作界面
{
  let s=pptx.addSlide(); bg(s); header(s,8,'客户可在桌面端完成日常操作','页面围绕任务、结果和异常组织');
  const items=[
    ['首页','日历查看计划、运行状态和进度'],
    ['任务配置','启停定时任务或立即执行'],
    ['文件中心','打开文件、压缩包及历史版本'],
    ['执行日志','查看步骤失败原因并上传诊断包'],
    ['指标配置','维护工作流和单指标回放'],
    ['录制工坊','录制新流程并导出原始录制包']
  ];
  items.forEach((it,i)=>{let col=i%2,row=Math.floor(i/2),x=.78+col*6.04,y=2.30+row*1.22;txt(s,it[0],x,y,2.0,.48,19,C.teal,true);txt(s,it[1],x+1.93,y,3.93,.63,16,C.ink);line(s,x,y+.78,x+5.63,y+.78,C.line,.8);});
}

// 9 — 验收与决策
{
  let s=pptx.addSlide(); bg(s); header(s,9,'建议按现场结果完成交付验收','先确认业务口径，再用真实 U-Key 和客户账号演示完整任务');
  txt(s,'建议验收项',.76,2.30,3.4,.45,20,C.navy,true);
  const a=['专网接入与登录成功','选定指标按指定日期采集','下载文件可打开且落入约定目录','登录/网络/导出失败正确暂停或标失败','日历、日志、历史版本可核查'];
  a.forEach((v,i)=>{txt(s,String(i+1).padStart(2,'0'),.82,2.95+i*.57,.62,.32,15,C.teal,true);txt(s,v,1.52,2.91+i*.57,5.5,.42,16,C.ink);});
  line(s,7.19,2.31,7.19,6.31,C.line,1);
  txt(s,'需客户确认',7.64,2.30,4.2,.45,20,C.navy,true);
  const b=['采集指标清单与账号权限','日期按自然日还是交易日计算','交付目录、文件命名与历史版本使用习惯','异常通知接收人及通道','是否需要联网上传和远程支持'];
  b.forEach((v,i)=>{rect(s,7.67,2.99+i*.58,.08,.24,C.teal);txt(s,v,7.95,2.90+i*.58,4.6,.48,15.5,C.ink);});
  txt(s,'当前验证依据：项目代码、交付说明和 5 个 mocksite 工作流回归；真实平台效果以现场验收为准。',.82,6.54,11.75,.34,12,C.grey);
}

const out=path.join(__dirname,'广东电力交易数据采集解决方案.pptx');
pptx.writeFile({fileName:out});
