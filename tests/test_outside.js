const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

// Mock globals
let mocks = {};
global.$ = (id) => mocks[id] || ({
    id,
    textContent: '',
    replaceChildren: function(...nodes) { this.children = nodes; }
});
global.el = (tag, attrs, ...children) => ({tag, attrs, children});
global.chip = (text, type) => ({text, type});

// Load outside.js
const code = fs.readFileSync('tutti/web/outside.js', 'utf8');
vm.runInThisContext(code);

function testGoodCase() {
    mocks = {
        'outside': {
            children: [],
            replaceChildren: function(...nodes) { this.children = nodes; }
        },
        'outside-meta': { textContent: '' }
    };
    const s = {
        outside: {
            hours: 1,
            commits: [{ repo: 'repo1', sha: 'sha1', author: 'author1', subject: 'subj1' }],
            leases: [{ kind: 'kind1', name: 'name1', why: 'why1', who: 'who1' }],
            unreadable: []
        }
    };
    renderOutside(s);
    assert.strictEqual(mocks['outside-meta'].textContent, '2 in the last 1 h');
    assert.strictEqual(mocks['outside'].children.length, 2);
    console.log('Good case passed');
}

function testBadCase() {
    mocks = {
        'outside': {
            children: [],
            replaceChildren: function(...nodes) { this.children = nodes; }
        },
        'outside-meta': { textContent: '' }
    };
    const s = {
        outside: {
            hours: 1,
            commits: [],
            leases: [],
            unreadable: ['repo_err']
        }
    };
    renderOutside(s);
    assert.strictEqual(mocks['outside-meta'].textContent, 'none in the last 1 h');
    assert.strictEqual(mocks['outside'].children.length, 1);
    assert.strictEqual(mocks['outside'].children[0].children[1].text, 'not read');
    console.log('Bad case passed');
}

function testNullCase() {
    renderOutside(null);
    console.log('Null case passed');
}

function testNoOutsideElement() {
    mocks = {};
    renderOutside({ outside: { hours: 1, commits: [], leases: [], unreadable: [] } });
    console.log('No outside element case passed');
}

function testAllThroughTutti() {
    mocks = {
        'outside': {
            children: [],
            replaceChildren: function(...nodes) { this.children = nodes; }
        },
        'outside-meta': { textContent: '' }
    };
    const s = {
        outside: {
            hours: 1,
            commits: [],
            leases: [],
            unreadable: []
        }
    };
    renderOutside(s);
    assert.strictEqual(mocks['outside-meta'].textContent, 'none in the last 1 h');
    assert.strictEqual(mocks['outside'].children.length, 1);
    assert.strictEqual(mocks['outside'].children[0].children[0].text, 'all through Tutti');
    console.log('All through Tutti case passed');
}

testGoodCase();
testBadCase();
testNullCase();
testNoOutsideElement();
testAllThroughTutti();
