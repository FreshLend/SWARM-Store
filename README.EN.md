# SWARM-Store [RU](README.md) | **EN**

The official repository of modules and themes for [SWARM](https://github.com/FreshLend/SWARM).

This repository serves as a central hub where can browse, download, and install extensions for their SWARM desktop environment directly through the built-in Store module.

### Modules
Modules must be placed inside the `modules` directory. You can create any category folders you like to organize them. 

The required path format is:
`modules/<category>/<module_name>/`

Inside the `<module_name>` folder, you must have:
*   `metadata.json` (must contain at least: `name`, `version`, `author`, `description`).
*   `module.py` (the main module class).
*   *(Optional)* `icon.svg` or `icon.png`.
*   *(Optional)* A `langs` folder with translation files.

### Themes
Themes must be placed inside the `styles` directory, following the same categorization logic.

The required path format is:
`styles/<category>/<theme_name>/`

Inside the `<theme_name>` folder, you must have:
*   `theme.json` (must contain at least: `name`, `version`, `author`).
*   `style.qss` (the Qt stylesheet template).

## Adding a Custom Repository

If you are developing your own modules and want to test them or share them with a specific group, you do not need to create a pull request here. 

You can create your own GitHub repository following the path rules described above. Then, in the SWARM Store module, simply click **Add Repo**, enter your repository name in the `owner/repo` format, and it will appear in your store.

## Ecosystem
* [SWARM](https://github.com/FreshLend/SWARM) - The core platform.
* [O.N.Y.X.](https://github.com/FreshLend/O.N.Y.X.) - Network analysis toolkit built on SWARM.
