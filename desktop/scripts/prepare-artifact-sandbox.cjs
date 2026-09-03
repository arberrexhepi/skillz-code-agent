const load = require('./load-ts.cjs');
const {ensureSandboxImage} = load(() => require('../src/main/services/artifactSandbox.ts'));
ensureSandboxImage(text => process.stdout.write(text), require('node:path').resolve(__dirname, '../..')).then(value => console.log(value.image)).catch(error => { console.error(error); process.exitCode = 1; });
