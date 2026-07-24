export default {
  extends: ['@commitlint/config-conventional'],
  helpUrl: 'https://www.conventionalcommits.org/ru/v1.0.0/',
  rules: {
    'header-max-length': [2, 'always', 100],
    'body-max-line-length': [1, 'always', 200],
  },
}
