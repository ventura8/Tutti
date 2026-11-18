
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const SEP = /^\s*\|[: \-|]+\|\s*$/;

console.log('| col1 | col2 | matches?', SEP.test('| col1 | col2 |'));
console.log('| --- | --- | matches?', SEP.test('| --- | --- |'));
console.log('| val1 | val2 | matches?', SEP.test('| val1 | val2 |'));
