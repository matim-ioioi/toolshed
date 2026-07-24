import createConventionalCommitsPreset from 'conventional-changelog-conventionalcommits'

/** @typedef {import('conventional-changelog-conventionalcommits').ConventionalCommit} Commit */
/** @typedef {{ title: string, commits: Commit[] }} CommitGroup */
/**
 * @typedef {{
 *   commitGroups: CommitGroup[],
 *   noteGroups: unknown[],
 *   version: string,
 *   host: string,
 *   owner: string,
 *   repository: string,
 *   previousTag?: string | null,
 *   currentTag?: string | null,
 *   linkCompare?: boolean,
 * }} RenderContext
 */

const TIER_1_TYPES = [
  { type: 'feat', section: 'Features' },
  { type: 'fix', section: 'Bug Fixes' },
  { type: 'perf', section: 'Performance Improvements' },
  { type: 'refactor', section: 'Code Refactoring' },
  { type: 'revert', section: 'Reverts' },
  { type: 'docs', section: 'Documentation' },
]

const OTHER_CHANGES_SECTION = 'Other Changes'
const OTHER_CHANGES_TYPE = '__other_changes__'

const TIER_2_TYPES = [
  { type: 'style', section: 'Styles' },
  { type: 'test', section: 'Tests' },
  { type: 'build', section: 'Build System' },
  { type: 'ci', section: 'Continuous Integration' },
  { type: 'chore', section: 'Chores' },
]

const sectionOrder = [
  ...TIER_1_TYPES.map((entry) => entry.section),
  OTHER_CHANGES_SECTION,
  ...TIER_2_TYPES.map((entry) => entry.section),
]

const isTier2Section = Object.fromEntries(
  TIER_2_TYPES.map((entry) => [entry.section, true])
)

const knownTypes = new Set([...TIER_1_TYPES, ...TIER_2_TYPES].map((entry) => entry.type))

// {{host}}/{{owner}}/{{repository}} приходят из контекста релиза (release-notes-generator
// сам разбирает repositoryUrl и кладёт их в корень контекста) — ссылки собираются
// Handlebars-выражениями, а не строкой, зафиксированной на момент загрузки конфига,
// потому что до вызова генератора хост/owner/repo ещё не известны.
const headerPartial = `## {{#if linkCompare~}}[{{version}}]({{host}}/{{owner}}/{{repository}}/compare/{{previousTag}}...{{currentTag}}){{~else}}[{{version}}]{{~/if}} - {{date}}

`

// commitPartial рендерится внутри {{#each commits}} для каждого коммита отдельно,
// поэтому корневые поля контекста (host/owner/repository) доступны только через
// root — параметр партиала, явно прокинутый из mainTemplate как `root=@root`.
const commitPartial = '- {{#if scope}}**{{scope}}:** {{/if}}{{#if subject}}{{subject}}{{else}}{{header}}{{/if}} ([{{shortHash}}]({{root.host}}/{{root.owner}}/{{root.repository}}/commit/{{hash}}))'

const mainTemplate = `{{> header}}{{#each noteGroups}}{{#unless @first}}

{{/unless}}### ⚠ {{title}}

{{#each notes}}{{#unless @first}}
{{/unless}}- {{#if commit.scope}}**{{commit.scope}}:** {{/if}}{{text}}{{/each}}{{/each}}{{#if noteGroups}}

{{/if}}{{#each commitGroups}}{{#unless (lookup @root.isTier2Section title)}}{{#unless @first}}

{{/unless}}### {{title}}

{{#each commits}}{{#unless @first}}
{{/unless}}{{> commit root=@root}}{{/each}}{{/unless}}{{/each}}{{#if @root.tier2Groups.length}}{{#if @root.hasContentBeforeMaintenance}}

{{/if}}<details>
<summary>Maintenance</summary>

{{#each @root.tier2Groups}}{{#unless @first}}

{{/unless}}### {{title}}

{{#each commits}}{{#unless @first}}
{{/unless}}{{> commit root=@root}}{{/each}}{{/each}}

</details>
{{/if}}
`

/**
 * @param {CommitGroup} groupA
 * @param {CommitGroup} groupB
 * @returns {number}
 */
function sortCommitGroupsByTier(groupA, groupB) {
  return sectionOrder.indexOf(groupA.title) - sectionOrder.indexOf(groupB.title)
}

/**
 * Достраивает контекст рендера: выделяет Tier 2 группы отдельным списком, чтобы
 * `@first`/`@last` внутри блока `<details>` считались от их собственной подвыборки,
 * а не от индекса в полном commitGroups (иначе разделитель ломается, когда релиз
 * состоит из одних Tier 2 коммитов), и прокидывает карту Tier 2 секций в шаблон.
 *
 * В отличие от версии для conventional-changelog-core, previousTag/currentTag/
 * linkCompare здесь не пересчитываются: semantic-release/release-notes-generator
 * уже кладёт их в context ДО вызова writer'а (из lastRelease.gitTag/gitHead и
 * nextRelease.gitTag/gitHead), поэтому просто сохраняем то, что пришло через
 * `...context` — переопределение этих полей самодельной логикой gitSemverTags
 * из core-пайплайна здесь бессмысленно (такого поля в контексте попросту нет)
 * и рискует молча стереть готовую ссылку сравнения.
 * @param {RenderContext} context
 * @returns {RenderContext & { tier2Groups: CommitGroup[], hasContentBeforeMaintenance: boolean }}
 */
function finalizeContext(context) {
  const tier2Groups = context.commitGroups.filter((group) => isTier2Section[group.title])
  const hasContentBeforeMaintenance = context.commitGroups.length > tier2Groups.length

  return {
    ...context,
    isTier2Section,
    tier2Groups,
    hasContentBeforeMaintenance,
  }
}

const basePreset = createConventionalCommitsPreset({
  types: [
    ...TIER_1_TYPES,
    ...TIER_2_TYPES,
    { type: OTHER_CHANGES_TYPE, section: OTHER_CHANGES_SECTION },
  ],
  ignoreCommits: /^chore\(release\):/,
})

// mainTemplate компилируется writer'ом с noEscape: true (это нужно, чтобы
// <details><summary>Maintenance</summary> из mainTemplate доходил до вывода как есть),
// поэтому subject/header/scope и текст BREAKING CHANGES — все они приходят из
// коммитов контрибьюторов — экранируются здесь, на уровне данных, а не в шаблоне.
/**
 * @param {string | undefined} value
 * @returns {string | undefined}
 */
function escapeHtml(value) {
  return typeof value === 'string'
    ? value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    : value
}

// notes[].text — единственное многострочное поле, попадающее в шаблон: `BREAKING
// CHANGE:`-футер несёт тело коммита целиком, с переносами строк. Внутри списка
// (mainTemplate:69) перевод строки без отступа завершает `- `-элемент по правилам
// CommonMark, и вставленный в футер текст с `\n\n### Заголовок` на нулевой колонке
// рендерится как настоящий заголовок вне списка. Отступ в два пробела — ширина
// маркера `- ` — держит продолжение внутри элемента списка вместо схлопывания
// строк, которое обрезало бы легитимное многоабзацное описание breaking change.
/**
 * @param {string | undefined} value
 * @returns {string | undefined}
 */
function escapeAndIndentNoteText(value) {
  if (typeof value !== 'string') {
    return value
  }

  return escapeHtml(value)
    .split(/\r\n|\r|\n/)
    .map((line, index) => (index === 0 || line.trim() === '' ? line : `  ${line}`))
    .join('\n')
}

/**
 * Форма объекта, который реально возвращает writer.transform() пресета
 * conventional-changelog-conventionalcommits — шире, чем `object`, объявленный
 * в types/conventional-changelog-conventionalcommits.d.ts (та декларация умышленно
 * покрывает только поля самого коммита, не возврат transform).
 * @typedef {{
 *   notes: Array<{ text?: string }>,
 *   subject?: string,
 *   scope?: string,
 * }} WriterTransformPatch
 */

/**
 * @param {Commit} commit
 * @param {object} context
 * @returns {object | undefined}
 */
function transform(commit, context) {
  const type = commit.type ? commit.type.toLowerCase() : commit.type
  const routedCommit = type && !knownTypes.has(type)
    ? { ...commit, type: OTHER_CHANGES_TYPE }
    : commit

  const patch = /** @type {WriterTransformPatch | undefined} */ (basePreset.writer.transform(routedCommit, context))
  if (!patch) {
    return patch
  }

  return {
    ...patch,
    header: escapeHtml(commit.header),
    subject: escapeHtml(patch.subject),
    scope: escapeHtml(patch.scope),
    notes: patch.notes.map((note) => ({ ...note, text: escapeAndIndentNoteText(note.text) })),
  }
}

/**
 * Точка входа для `@semantic-release/release-notes-generator` (опция `config`
 * в .releaserc.json). Экспортируется как функция без аргументов — это
 * единственный способ доставить в JSON-конфиг функции (transform,
 * finalizeContext, commitGroupsSort), которые сам JSON нести не может.
 * @returns {object}
 */
export default function loadConfig() {
  return {
    ...basePreset,
    writer: {
      ...basePreset.writer,
      mainTemplate,
      headerPartial,
      commitPartial,
      commitGroupsSort: sortCommitGroupsByTier,
      commitsSort: false,
      transform,
      finalizeContext,
    },
  }
}
