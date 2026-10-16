const [base, id, token] = process.argv.slice(2);
const h = { Authorization: `Bearer ${token}` };
for (const p of [`/api/sessions/${id}`, `/api/sessions/${id}/events`, `/api/sessions/${id}/view`]) {
  try {
    const r = await fetch(base + p, { headers: h });
    const t = await r.text();
    console.log(`${p}  ->  HTTP ${r.status}`);
    console.log('   ' + t.slice(0, 700).replace(/\n/g, '\n   '));
  } catch (e) { console.log(`${p}  ->  失败 ${e.message}`); }
  console.log('');
}