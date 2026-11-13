import { copyFileSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const uiRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const lock = JSON.parse(readFileSync(path.join(uiRoot, 'package-lock.json'), 'utf8'))
const publicRoot = path.join(uiRoot, 'public')
const licenseRoot = path.join(publicRoot, 'licenses')
mkdirSync(licenseRoot, { recursive: true })

const packages = Object.entries(lock.packages)
  .filter(([packagePath, info]) => packagePath.startsWith('node_modules/') && info.dev !== true && info.devOptional !== true)
  .map(([packagePath]) => {
    const root = path.join(uiRoot, packagePath)
    const metadata = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'))
    const licenseFilename = readdirSync(root).find((filename) => /^(license|licence|copying)(\.|$)/i.test(filename))
    const licenseText = licenseFilename === undefined ? null : readFileSync(path.join(root, licenseFilename), 'utf8').trim()
    return {
      name: metadata.name,
      version: metadata.version,
      license: typeof metadata.license === 'string' ? metadata.license : JSON.stringify(metadata.license ?? 'Unspecified'),
      licenseText,
    }
  })
  .sort((left, right) => left.name.localeCompare(right.name))

const generatedUiLicense = `MIT License

Copyright (c) 2023 shadcn

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.`

const licenseGroups = new Map()
for (const dependency of packages) {
  if (dependency.licenseText === null) continue
  const group = licenseGroups.get(dependency.licenseText) ?? []
  group.push(`${dependency.name}@${dependency.version}`)
  licenseGroups.set(dependency.licenseText, group)
}

const packageList = packages.map((dependency) =>
  `- ${dependency.name}@${dependency.version}: ${dependency.license}${dependency.licenseText === null ? ' (license file not shipped in package)' : ''}`,
).join('\n')
const licenseSections = [...licenseGroups.entries()].map(([text, names], index) =>
  `\n\n--- Package license ${index + 1} ---\nPackages: ${names.join(', ')}\n\n${text}`,
).join('')
const notices = [
  'Third-party notices for the factory progress UI',
  '',
  'Generated from ui/package-lock.json by `npm run notices`. Development-only packages are excluded.',
  '',
  'Generated UI components',
  'The generated components in ui/src/components/ui are adapted from shadcn/ui and use the MIT License.',
  'Project: https://github.com/shadcn-ui/ui',
  generatedUiLicense,
  '',
  'Production dependency packages',
  packageList,
  licenseSections,
  '',
].join('\n')

writeFileSync(path.join(publicRoot, 'third-party-notices.txt'), notices)
copyFileSync(path.join(uiRoot, 'node_modules/@fontsource-variable/geist/LICENSE'), path.join(licenseRoot, 'geist-LICENSE.txt'))
