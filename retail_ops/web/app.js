"use strict";
const $ = id => document.getElementById(id);
let current = null, currentReport = "issues", photoResult = null;
const labels = {source:"Файл",row:"Строка",shipment_id:"Поставка",supplier:"Поставщик",sku:"Артикул",brand:"Бренд",name:"Наименование",color:"Цвет",size:"Размер",quantity:"Количество",gtin:"GTIN",ntin:"NTIN",material:"Материал",country:"Страна",severity:"Уровень",code:"Проверка",field:"Поле",message:"Замечание",expected:"Эталон",actual:"В файле",status:"Статус",_source:"Источник",_row:"Строка",_status:"Статус строки",lines:"Строк",quantity_known:"Известное количество",quantity_invalid_lines:"Строк с неверным количеством",blocked_lines:"Строк с ошибками",suppliers:"Поставщики",codes:"Кодов КМ",difference:"Отклонение",original_name:"Исходное имя",new_name:"Новое имя",bytes:"Байт",sha256:"SHA-256"};
const statusLabels = {error:"Ошибка",warning:"Замечание",draft_ready:"Готов как черновик",blocked:"Есть ошибки",draft:"Черновик",match:"Совпадает",shortage:"Не хватает КМ",excess:"Избыток КМ",orphan:"Нет в поставке",not_checked:"КМ не загружены",source_invalid:"Ошибки в поставке",code_invalid:"Ошибки в файле КМ"};
async function api(path, payload) {
 const response = await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Local-Token":document.querySelector('meta[name="local-token"]').content},body:JSON.stringify(payload)});
 const data = await response.json(); if(!response.ok) throw new Error(data.error || "Ошибка обработки"); return data;
}
async function readFile(file) {
 if(!file) return null;
 if(file.size>15*1024*1024) throw new Error(file.name+": файл больше 15 МБ");
 const buffer = new Uint8Array(await file.arrayBuffer());
 let binary=""; for(let i=0;i<buffer.length;i+=32768) binary+=String.fromCharCode(...buffer.subarray(i,i+32768));
 return {name:file.name,content:btoa(binary)};
}
function download(base64,name){ const bytes=Uint8Array.from(atob(base64),c=>c.charCodeAt(0));const url=URL.createObjectURL(new Blob([bytes],{type:"application/zip"}));const link=document.createElement("a");link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000); }
function metric(parent,value,label,warning=false){const box=document.createElement("div");box.className="metric"+(warning?" error-metric":"");const strong=document.createElement("strong"),span=document.createElement("span");strong.textContent=value;span.textContent=label;box.append(strong,span);parent.append(box);}
function renderTable(container, rows, total=rows.length){
 container.replaceChildren();
 if(!total){const p=document.createElement("p");p.className="small";p.textContent="В этом отчёте нет записей.";container.append(p);return;}
 if(!rows.length){container.textContent="Совпадений не найдено.";return;}
 const fields=[...new Set(rows.flatMap(Object.keys))];const table=document.createElement("table"),head=document.createElement("thead"),tr=document.createElement("tr");
 fields.forEach(k=>{const th=document.createElement("th");th.textContent=labels[k]||k;tr.append(th);});head.append(tr);table.append(head);const body=document.createElement("tbody");
 rows.slice(0,500).forEach(row=>{const tr=document.createElement("tr");fields.forEach(k=>{const td=document.createElement("td");const v=row[k]??"";td.textContent=["status","_status","severity"].includes(k)?statusLabels[v]||v:String(v);tr.append(td);});body.append(tr);});table.append(body);container.append(table);
}
function showReport(){if(!current)return;const all=current.reports[currentReport]||[];const query=$("search").value.toLocaleLowerCase();const filtered=all.filter(r=>Object.values(r).some(v=>String(v).toLocaleLowerCase().includes(query)));renderTable($("table"),filtered,all.length);$("table-count").textContent=`Найдено: ${filtered.length} из ${all.length}. На экране до 500 строк.`;}
function showResult(data){current=data;$("results").hidden=false;$("metrics").replaceChildren();const s=data.summary;metric($("metrics"),s.rows,"Строк проверено");metric($("metrics"),s.draft_ready,"Готовы как черновик");metric($("metrics"),s.blocked,"Строк с ошибками",true);metric($("metrics"),s.warnings,"Замечаний");showReport();$("results").scrollIntoView({behavior:"smooth",block:"start"});}
async function action(button,errorId,fn){button.disabled=true;const error=$(errorId);error.hidden=true;try{await fn();}catch(e){error.textContent=e.message;error.hidden=false;}finally{button.disabled=false;}}
document.querySelectorAll(".nav").forEach(button=>button.addEventListener("click",()=>{document.querySelectorAll(".nav").forEach(b=>b.classList.toggle("active",b===button));document.querySelectorAll(".page").forEach(p=>p.hidden=p.id!=="page-"+button.dataset.page);window.scrollTo(0,0);}));
$("supplies").addEventListener("change",()=>$("supply-names").textContent=[...$("supplies").files].map(f=>f.name).join(", ")||"Файлы пока не выбраны");
$("demo").addEventListener("click",()=>action($("demo"),"data-error",async()=>showResult(await api("/api/demo",{}))));
$("analyze").addEventListener("click",()=>action($("analyze"),"data-error",async()=>{
 if(!$("supplies").files.length)throw new Error("Выберите файлы поставщиков или запустите пример.");
 const payload={supplies:await Promise.all([...$("supplies").files].map(readFile)),reference:await readFile($("reference").files[0]),codes:await readFile($("codes").files[0]),require_ntin:$("require-ntin").checked};
 if($("mapping").value.trim())payload.aliases=JSON.parse($("mapping").value);
 showResult(await api("/api/analyze",payload));
}));
document.querySelectorAll(".report-tab").forEach(button=>button.addEventListener("click",()=>{currentReport=button.dataset.report;document.querySelectorAll(".report-tab").forEach(b=>{b.classList.toggle("active",b===button);b.setAttribute("aria-selected",b===button?"true":"false");});showReport();}));
$("search").addEventListener("input",showReport);$("download").addEventListener("click",()=>current&&download(current.zip_base64,"retail-ops-report.zip"));
$("rename").addEventListener("click",()=>action($("rename"),"photo-error",async()=>{if(!$("photos").files.length||!$("manifest").files.length)throw new Error("Выберите ZIP фотографий и таблицу соответствий.");photoResult=await api("/api/photos",{photos:await readFile($("photos").files[0]),manifest:await readFile($("manifest").files[0])});$("photo-result").hidden=false;$("photo-count").textContent=`Фотографий подготовлено: ${photoResult.files.length}`;renderTable($("photo-table"),photoResult.files);}));
$("photo-download").addEventListener("click",()=>photoResult&&download(photoResult.zip_base64,"prepared-photos.zip"));
const defaultTasks=[["Таблицы и шаблоны",150,80],["Проверка товарных данных",90,50],["Подготовка фотографий",60,90],["Маркировка и документы",120,30],["Общение и исключения",60,0]];
defaultTasks.forEach(([name,minutes,percent],i)=>{const row=document.createElement("div");row.className="task-row";const label=document.createElement("label");label.htmlFor="minutes-"+i;label.textContent=name;row.append(label);for(const [kind,value,max]of [["minutes",minutes,1440],["percent",percent,100]]){const input=document.createElement("input");input.id=kind+"-"+i;input.type="number";input.min="0";input.max=String(max);input.value=String(value);input.setAttribute("aria-label",name+": "+(kind==="minutes"?"минут в день":"доля автоматизации в процентах"));row.append(input);}$("tasks").append(row);});
async function estimate(){const tasks=defaultTasks.map(([name],i)=>{const m=$("minutes-"+i).value,p=$("percent-"+i).value;if(m===""||p==="")throw new Error("Заполните все поля расчёта.");return {name,minutes:Number(m),fraction:Number(p)/100};});const r=await api("/api/estimate",{tasks,days:Number($("days").value)});$("estimate-result").replaceChildren();metric($("estimate-result"),r.saved_percent+"%","Времени в этом сценарии");metric($("estimate-result"),r.saved_minutes+" мин","В день освобождается");metric($("estimate-result"),r.after_minutes+" мин","Остаётся ручной работы");metric($("estimate-result"),r.monthly_hours+" ч","В месяц освобождается");}
$("estimate").addEventListener("click",()=>action($("estimate"),"estimate-error",estimate));
action($("estimate"),"estimate-error",estimate);
