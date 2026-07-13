// 励志短语（中文 + 信达雅英文），用于登录页 / 孩子学习页展示
// 来源：中华经典劝学诗文（信达雅直译，非机翻），适合初中生语境
// 用法：静态预置，运行时【不】调用任何 API；由前端按 30 分钟时间窗口选取
// 状态：候选稿，待人工复核后内联进 index.html / deploy-static/index.html
const MOTTOES = [
  { zh: '日积跬步，以致千里', en: 'A thousand-mile journey is made of single steps, gathered day by day.' },
  { zh: '疾风知劲草',         en: 'Only the driving wind reveals the resilient grass.' },
  { zh: '熟能生巧',           en: 'Practice breeds mastery.' },
  { zh: '温故而知新',         en: 'By revisiting the old, we come to know the new.' },
  { zh: '书山有路勤为径，学海无涯苦作舟', en: 'Diligence is the path through the mountain of books; hard work the boat across the sea of learning.' },
  { zh: '不积小流，无以成江海', en: 'Without gathering small streams, no river or sea can be formed.' },
  { zh: '业精于勤，荒于嬉',   en: 'Mastery is born of diligence and lost to distraction.' },
  { zh: '锲而不舍，金石可镂', en: 'Carve without cease, and even metal and stone can be engraved.' },
  { zh: '学而不思则罔，思而不学则殆', en: 'To learn without thought is to be lost; to think without learning is to be in peril.' },
  { zh: '绳锯木断，水滴石穿', en: 'A cord saws through wood; a single drop wears through stone.' },
  { zh: '宝剑锋从磨砺出，梅花香自苦寒来', en: 'A sword’s edge is honed by grinding; a plum’s fragrance comes from the cold.' },
  { zh: '少壮不努力，老大徒伤悲', en: 'If youth will not strive, age will mourn in vain.' }
];

// 预览用：在浏览器控制台打印，便于本地核对
if (typeof console !== 'undefined') {
  // console.log('[mottos] loaded', MOTTOES.length, 'entries');
}
