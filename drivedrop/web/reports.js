'use strict';
const openedDevices=new Set(), folderQueries=new Map(), folderLimits=new Map();
let deviceFilter='active', devicePage=0;
const number=n=>(n??0).toLocaleString('vi-VN');
function needsAttention(d){return !d.online||['error','stopped'].includes(d.status.state)||d.status.failed>0;}
function stateBadge(d){const s=d.status;return badge(d.revoked?'Đã khóa':!d.received?'Chưa gửi trạng thái':!d.online?'Mất kết nối':labels[s.state]||'Đang kết nối',d.revoked?'':!d.online?'warn':s.state==='error'?'bad':'good');}
function reportStats(){
 const s=data.report.summary,stats=$('#report-stats');stats.replaceChildren();
 const entries=[['Nhân viên',s.employees,s.unassigned?`${s.unassigned} máy chưa gán nhân viên`:'Đếm theo mã nhân viên'],['Máy đang kết nối',`${s.online} / ${s.devices}`,'Trên tổng máy được cấp quyền'],['Cần kiểm tra',s.attention,'Mất kết nối, dừng hoặc có lỗi'],['Đã lên Drive hôm nay',s.today_files,`${number(s.today_images)} ảnh · ${number(s.today_videos)} video`],['Thư mục trên máy',s.folders,`${s.inventory_machines} / ${s.devices} máy có báo cáo`],['Ảnh / video trên máy',`${number(s.images)} / ${number(s.videos)}`,`${size(s.bytes)} · theo lần quét gần nhất`]];
 for(const [title,value,detail] of entries){const a=el('article');a.append(el('span',title),el('strong',typeof value==='number'?number(value):value),el('small',detail));stats.append(a);}
 const overview=$('#report-overview');overview.replaceChildren();overview.hidden=tab!=='devices';
 if(tab!=='devices')return;
 const trend=el('section',undefined,'report-box'),head=el('div',undefined,'section-heading');head.append(el('h2','Upload đã xác minh'),badge('7 ngày · giờ Việt Nam'));trend.append(head);
 const chart=el('div',undefined,'trend-chart'),max=Math.max(1,...data.report.trend.map(x=>x.files));
 for(const day of data.report.trend){const c=el('div',undefined,'trend-day'),bar=el('div',undefined,'bar-track'),fill=el('div',undefined,'bar-fill');fill.style.height=(day.files/max*100)+'%';bar.append(fill);c.append(el('strong',number(day.files)),bar,el('span',day.date));c.title=`${day.date}: ${number(day.files)} tệp · ${size(day.bytes)}`;chart.append(c);}
 trend.append(chart,el('p','Chỉ tính tệp xác minh sau khi nâng cấp báo cáo. Không tính máy đã khóa.','hint'));
 const alerts=el('section',undefined,'report-box'),aHead=el('div',undefined,'section-heading');aHead.append(el('h2','Tình hình đội ngũ'),button('Xem máy cần kiểm tra',()=>{deviceFilter='attention';devicePage=0;render();},'quiet'));alerts.append(aHead);
 const active=data.devices.filter(d=>!d.revoked),working=active.filter(d=>d.online&&['uploading','hashing','scanning','verifying'].includes(d.status.state)).length;
 for(const [label,value,kind] of [['Đang xử lý tệp',working,'good'],['Mất kết nối / chưa chạy',active.filter(d=>!d.online).length,'warn'],['Đang báo lỗi',active.filter(d=>d.status.state==='error'||d.status.failed>0).length,'bad'],['Báo cáo thư mục còn mới',`${s.inventory_fresh} / ${s.devices}`,'good']]){const r=el('div',undefined,'health-row');r.append(el('span',label),badge(String(value),kind));alerts.append(r);}
 if(s.inventory_partial)alerts.append(el('p',`${s.inventory_partial} máy quét chưa đầy đủ. Tổng thư mục và ảnh/video mới tính phần đã quét được.`,'hint'));
 alerts.append(el('p','Trạng thái phản ánh ứng dụng gửi file, không đo thời gian làm việc của nhân viên.','hint'));overview.append(trend,alerts);
}
function profileDialog(d){
 const f=$('#profile-form');f.elements.id.value=d.id;f.elements.machine_name.value=d.name;f.elements.employee_code.value=d.employee_code;f.elements.employee_name.value=d.employee_name;f.querySelector('.error').textContent='';
 const list=$('#employee-options');list.replaceChildren();const seen=new Set();data.devices.forEach(x=>{if(x.employee_code&&!seen.has(x.employee_code)){const o=el('option');o.value=x.employee_code;o.label=x.employee_name;list.append(o);seen.add(x.employee_code);}});$('#profile-dialog').showModal();
}
function renderFolders(d,box){
 const inv=d.status.inventory;
 if(!inv){box.append(empty('Chưa có báo cáo thư mục',d.received?'Cập nhật DriveDrop trên Mac lên 0.5.0 và bật chạy nền.':'Máy chưa gửi trạng thái. Mở DriveDrop trên máy nhân viên để kết nối.'));return;}
 const bar=el('div',undefined,'folder-heading'),left=el('div');left.append(el('strong',inv.root),el('small',`Quét lúc ${when(inv.captured)} · ${inv.folders.length-1} thư mục con`));bar.append(left);
 if(!d.inventory_fresh)bar.append(badge('Dữ liệu đã cũ','warn'));
 if(!inv.complete)bar.append(badge('Quét chưa đầy đủ','warn'));
 box.append(bar);
 const total=d.inventory_totals,summary=el('div',undefined,'inventory-summary');summary.append(el('span',`${number(total.images)} ảnh`),el('span',`${number(total.videos)} video`),el('span',`${number(total.other)} tệp khác`),el('span',size(total.bytes)));box.append(summary);
 const search=el('input');search.placeholder='Tìm thư mục trong máy này…';search.setAttribute('aria-label','Tìm thư mục '+d.name);search.value=folderQueries.get(d.id)||'';search.dataset.folderSearch=d.id;search.oninput=()=>{folderQueries.set(d.id,search.value);folderLimits.set(d.id,50);render();};box.append(search);
 const q=search.value.trim().toLocaleLowerCase(),filtered=inv.folders.filter(f=>f.path.toLocaleLowerCase().includes(q)),limit=folderLimits.get(d.id)||50;
 box.append(table(['Thư mục','Ảnh','Video','Tệp khác','Dung lượng','Trong hàng đợi'],filtered.slice(0,limit).map(f=>{const path=el('span',f.path==='.'?'Tệp ngay trong thư mục gốc':f.path,'folder-path');return [cell(path),number(f.images),number(f.videos),number(f.other),size(f.bytes),number(f.queued)];})));
 if(!filtered.length)box.append(el('p','Không có thư mục khớp từ khóa.','hint'));
 if(filtered.length>limit)box.append(button(`Xem thêm (${filtered.length-limit} thư mục)`,()=>{folderLimits.set(d.id,limit+50);render();}));
 box.append(el('p','Mỗi dòng đếm tệp trực tiếp trong thư mục đó; thư mục con nằm ở dòng riêng. Tệp đang upload vẫn được tính về thư mục nguồn.','hint'));
 if(!inv.complete||inv.skipped)box.append(el('p',`Đã bỏ qua ${number(inv.skipped)} đường dẫn hoặc gặp giới hạn quét. Báo cáo tối đa 300 thư mục, 100.000 mục mỗi lần; không đọc thư mục ẩn và liên kết.`,'hint'));
}
function machineCard(d){
 const s=d.status,inv=s.inventory,t=d.inventory_totals,details=el('details',undefined,'machine');details.open=openedDevices.has(d.id);details.dataset.device=d.id;
 const summary=el('summary',undefined,'machine-summary'),name=el('div',undefined,'machine-name');name.append(el('span','›','chevron'),el('strong',d.name),el('small',when(d.received)));
 const media=el('div');media.append(el('strong',inv?`${inv.complete?'':'≥ '}${inv.folders.length-1} thư mục`:'Chưa có báo cáo'),el('small',t?`${number(t.images)} ảnh · ${number(t.videos)} video${d.inventory_fresh?'':' · Dữ liệu cũ'}`:'Cần Mac 0.5.0'));
 const today=el('div');today.append(el('strong',`${number(d.today_files)} tệp hôm nay`),el('small',`Tổng đã xác minh: ${number(d.confirmed)}`));
 summary.append(name,stateBadge(d),media,today);details.append(summary);details.ontoggle=()=>{if(details.open)openedDevices.add(d.id);else openedDevices.delete(d.id);};
 const body=el('div',undefined,'machine-body'),activity=el('div',undefined,'activity'),p=s.size?Math.min(100,Math.round(s.sent/s.size*100)):0;
 activity.append(el('strong',s.file||'Chưa có tệp đang xử lý'),el('small',s.destination||`Phiên bản Mac: ${s.version||'Chưa có'}`));
 if(s.size){const progress=el('progress');progress.max=100;progress.value=p;progress.setAttribute('aria-label',`Tiến độ ${d.name}: ${p}%`);activity.append(progress,el('span',`${p}% · ${size(s.sent)} / ${size(s.size)}`));}
 activity.append(el('small',`Hàng đợi: ${number(s.queued)} · Tệp cần kiểm tra: ${number(s.failed)} · ${labels[s.state]||'Chưa hoạt động'}`));
 if(s.error)activity.append(el('p',s.error,'error'));body.append(activity);
 const actions=el('div',undefined,'device-actions');actions.append(button('Gán nhân viên / đổi tên máy',()=>profileDialog(d)));
 if(!d.revoked)actions.append(button('Khóa máy',()=>{if(confirm(`Khóa ${d.name}? Máy này sẽ không được cấp phiên upload và xác minh mới.`))act('/api/revoke',{id:d.id},'Đã khóa máy.');}));body.append(actions);
 let loaded=details.open;if(loaded)renderFolders(d,body);details.ontoggle=()=>{if(details.open){openedDevices.add(d.id);if(!loaded){renderFolders(d,body);loaded=true;}}else openedDevices.delete(d.id);};details.append(body);return details;
}
function renderDevices(content,q){
 const filters=el('div',undefined,'report-filters');for(const [id,label] of [['active','Đang được cấp quyền'],['online','Đang kết nối'],['attention','Cần kiểm tra'],['unassigned','Chưa gán nhân viên'],['revoked','Đã khóa']]){const b=button(label,()=>{deviceFilter=id;devicePage=0;render();},deviceFilter===id?'selected quiet':'quiet');b.setAttribute('aria-pressed',String(deviceFilter===id));filters.append(b);}content.append(filters);
 const rows=data.devices.filter(d=>deviceFilter==='revoked'?d.revoked:!d.revoked&&(deviceFilter==='active'||deviceFilter==='online'&&d.online||deviceFilter==='attention'&&needsAttention(d)||deviceFilter==='unassigned'&&!d.employee_code)).filter(d=>[d.name,d.employee_code,d.employee_name,...(d.status.inventory?.folders.map(f=>f.path)||[])].join(' ').toLocaleLowerCase().includes(q));
 if(!rows.length){content.append(empty(q?'Không tìm thấy máy phù hợp':deviceFilter==='active'?'Chưa có máy nhân viên':'Không có máy trong nhóm này',deviceFilter==='active'?'Cấp file kích hoạt rồi nhập trên Mac. Sau khi máy kết nối, gán mã nhân viên để báo cáo tổng nhân sự chính xác.':'Thử chọn nhóm khác hoặc xóa từ khóa tìm kiếm.',deviceFilter==='active'&&!q?button('Cấp máy đầu tiên',()=>$('#enroll-dialog').showModal(),'primary'):null));return rows;}
 rows.sort((a,b)=>(a.employee_code||'\uffff').localeCompare(b.employee_code||'\uffff')||a.name.localeCompare(b.name, 'vi'));
 devicePage=Math.min(devicePage,Math.ceil(rows.length/20)-1);const page=rows.slice(devicePage*20,devicePage*20+20),groups=new Map();
 page.forEach(d=>{const key=d.employee_code||'';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(d);});
 for(const [code,machines] of groups){const group=el('section',undefined,'employee-group'),all=data.devices.filter(d=>(d.employee_code||'')===code&&!d.revoked),heading=el('div',undefined,'employee-heading'),identity=el('div');identity.append(el('span',code?machines[0].employee_name.slice(0,1).toUpperCase():'?','avatar'),el('h3',code?machines[0].employee_name:'Chưa gán nhân viên'),badge(code||'Chưa tính vào tổng nhân sự',code?'':'warn'));
 heading.append(identity,el('span',`${all.length} máy được cấp quyền · ${number(all.reduce((n,d)=>n+d.today_files,0))} tệp hôm nay`));group.append(heading,...machines.map(machineCard));content.append(group);}
 if(rows.length>20){const nav=el('div',undefined,'pagination'),prev=button('← Trước',()=>{devicePage--;render();}),next=button('Sau →',()=>{devicePage++;render();});prev.disabled=devicePage===0;next.disabled=(devicePage+1)*20>=rows.length;nav.append(prev,el('span',`Trang ${devicePage+1} / ${Math.ceil(rows.length/20)}`),next);content.append(nav);}
 return rows;
}
function exportReport(){
 if(!data)return;const rows=[['Mã nhân viên','Nhân viên','Máy','Trạng thái','Thư mục','Ảnh trên máy','Video trên máy','Tệp khác','Byte','Hàng đợi','Lần quét','Đầy đủ','Tệp xác minh hôm nay (máy)']];
 data.devices.filter(d=>!d.revoked).forEach(d=>{const inv=d.status.inventory,folders=inv?.folders||[null];folders.forEach(f=>rows.push([d.employee_code,d.employee_name,d.name,d.online?labels[d.status.state]:'Mất kết nối',f?.path??'Chưa có báo cáo',f?.images??'',f?.videos??'',f?.other??'',f?.bytes??'',f?.queued??'',inv?when(inv.captured):'',inv?String(inv.complete):'',f===folders[0]?d.today_files:'']));});
 const csv='\ufeff'+rows.map(r=>r.map(x=>'"'+String(x??'').replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"').join(',')).join('\r\n'),url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'})),a=el('a');a.href=url;a.download='DriveDrop-bao-cao-'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
