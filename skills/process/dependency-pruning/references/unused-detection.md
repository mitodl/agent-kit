# Unused Dependency Detection — Per-Ecosystem Commands

## Python

### Primary: deptry

```bash
uvx deptry .
```

deptry reads `pyproject.toml` or `requirements.txt` and cross-references
imports across the codebase. No installation needed with `uvx`.

Exit codes: 0 = clean, 1 = issues found (check stdout for details).

Relevant codes:
- `DEP001` — dependency declared but never imported → removal candidate
- `DEP002` — import found but not declared (missing dep) → ignore for pruning
- `DEP003` — transitive dep used directly → note, don't remove
- `DEP004` — dev dep used in non-dev code → note as misclassification

```bash
# JSON output for parsing
uvx deptry . --json-output /tmp/deptry-out.json && cat /tmp/deptry-out.json
```

### Fallback: grep-based

```bash
# Read all declared deps from pyproject.toml
python -c "
import pathlib, subprocess, sys, re
try:
    import tomllib
except ImportError:
    try:
        import pip._vendor.tomli as tomllib
    except ImportError:
        print('Error: tomllib or tomli required'); sys.exit(1)

with open('pyproject.toml', 'rb') as f:
    data = tomllib.load(f)

deps = data.get('project', {}).get('dependencies', [])
pkgs = []
for d in deps:
    m = re.match(r'^([a-zA-Z0-9_.-]+)', d)
    if m:
        pkgs.append(m.group(1).lower().replace('-', '_'))

for pkg in pkgs:
    # Search including tests/ — test-only use is 'dev-only', not 'unused'
    result = subprocess.run(['rg', '-l', pkg, '--include=*.py'], capture_output=True, text=True)
    if not result.stdout.strip():
        print(f'UNUSED: {pkg}')
    else:
        files = result.stdout.strip().splitlines()
        test_only = all('test' in f for f in files)
        label = 'test-only' if test_only else 'used'
        print(f'{label}: {pkg} ({len(files)} files)')
"
```

---

## JavaScript / TypeScript

### Primary: depcheck

```bash
npx --yes depcheck --json 2>/dev/null
```

Output structure:
```json
{
  "dependencies": ["unused-pkg"],
  "devDependencies": ["unused-dev-pkg"],
  "missing": {},
  "invalidFiles": [],
  "invalidDirs": []
}
```

### Alternative: knip (better for monorepos)

```bash
npx --yes knip --reporter json 2>/dev/null
```

knip also finds unused exports, files, and type references — more thorough but
slower.

### Fallback: grep-based

```bash
node -e "
const pkg = require('./package.json');
const { execSync } = require('child_process');
const deps = Object.keys({...(pkg.dependencies||{}), ...(pkg.devDependencies||{})});
for (const dep of deps) {
  // Escape regex metacharacters (e.g. scoped packages like @org/name)
  const escaped = dep.replace(/[.*+?^\${}()|[\]\\\\]/g, '\\\\$&');
  try {
    const out = execSync(
      'rg -l \"' + escaped + '\" src/ -g \"*.ts\" -g \"*.tsx\" -g \"*.js\" -g \"*.jsx\"',
      {stdio:['pipe','pipe','pipe']}
    ).toString();
    console.log(out.trim() ? 'used:   ' + dep : 'UNUSED: ' + dep);
  } catch { console.log('UNUSED: ' + dep); }
}
"
```

---

## Go

### Check what go mod tidy would remove

Back up go.mod/go.sum, run tidy, capture output, then restore — non-destructive:

```bash
cp go.mod /tmp/go.mod.bak && cp go.sum /tmp/go.sum.bak
go mod tidy -v 2>&1 | grep "^removing"
cp /tmp/go.mod.bak go.mod && cp /tmp/go.sum.bak go.sum
```

Or use `go mod why` to check if a dep is reachable:

```bash
go mod why github.com/some/package 2>&1
# Output: "(main module does not need github.com/some/package)" → unused
```

List all direct deps:

```bash
go list -m -json all 2>/dev/null | python3 -c "
import json, sys
data = sys.stdin.read()
import re
for obj in re.split(r'\n(?=\{)', data.strip()):
    try:
        m = json.loads(obj)
        if not m.get('Indirect') and not m.get('Main'):
            print(m['Path'])
    except: pass
"
```

---

## Rust

### Primary: cargo-machete

```bash
cargo machete
```

If not installed:

```bash
cargo install cargo-machete --quiet 2>&1
cargo machete
```

### Alternative: cargo-udeps (requires nightly)

```bash
cargo +nightly udeps
```

### Fallback: grep the Cargo.toml deps against src/

```bash
python3 -c "
import pathlib, subprocess, sys
try:
    import tomllib
except ImportError:
    try:
        import pip._vendor.tomli as tomllib
    except ImportError:
        print('Error: tomllib or tomli required'); sys.exit(1)

cargo_data = tomllib.loads(pathlib.Path('Cargo.toml').read_text())
deps = []
for section in ['dependencies', 'dev-dependencies', 'build-dependencies']:
    deps.extend(cargo_data.get(section, {}).keys())

for dep in sorted(set(deps)):
    crate_name = dep.replace('-', '_')
    result = subprocess.run(['rg', '-l', crate_name, 'src/'], capture_output=True, text=True)
    if result.stdout.strip():
        print(f'used:   {dep}')
    else:
        result2 = subprocess.run(['rg', '-l', dep, 'src/'], capture_output=True, text=True)
        print(f'UNUSED: {dep}' if not result2.stdout.strip() else f'used:   {dep}')
"
```

---

## Other ecosystems

### Ruby (Gemfile)

```bash
bundle exec debundle 2>/dev/null || true
# Grep approach:
ruby -e "
require 'bundler'
Bundler.load.specs.each do |spec|
  next if spec.name == 'bundler'
  used = \`rg -l '#{spec.name}' lib/ app/ 2>/dev/null\`.strip
  puts used.empty? ? \"UNUSED: #{spec.name}\" : \"used: #{spec.name}\"
end
"
```

### Java/Kotlin (Maven)

```bash
mvn dependency:analyze 2>&1 | grep -E "Unused declared|Used undeclared"
```

### Java/Kotlin (Gradle)

```bash
./gradlew dependencies --configuration runtimeClasspath 2>/dev/null
# Then grep source for each dep
```

### Elixir (mix.exs)

```bash
mix deps.unlock --check-unused 2>&1
```
