#!/usr/bin/python
# This Python file uses the following encoding: utf-8

# Recipe Robot
# Copyright 2015 Elliot Jordan, Shea G. Craig, and Eldon Ahrold
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
recipe_generator.py

This module of Recipe Robot uses the facts collected by the main script to
create autopkg recipes for the specified app.
"""


import os

from .exceptions import RoboError
from .tools import (create_dest_dirs, create_existing_recipe_list,
                    create_SourceForgeURLProvider, extract_app_icon,
                    robo_print, LogLevel, __version__,
                    get_exitcode_stdout_stderr, timed, SUPPORTED_IMAGE_FORMATS,
                    SUPPORTED_ARCHIVE_FORMATS, SUPPORTED_INSTALL_FORMATS,
                    ALL_SUPPORTED_FORMATS, PREFS_FILE)

# TODO(Elliot): Can we use the one at /Library/AutoPkg/FoundationPlist instead?
# Or not use it at all (i.e. use the preferences system correctly). (#16)
try:
    from recipe_robot_lib import FoundationPlist
except ImportError:
    robo_print("Importing plistlib as FoundationPlist", LogLevel.WARNING)
    import plistlib as FoundationPlist


@timed
def generate_recipes(facts, prefs):
    """Generate the selected types of recipes.

    Args:
        facts: A continually-updated dictionary containing all the information
            we know so far about the app associated with the input path.
        prefs: The dictionary containing a key/value pair for each preference.
    """
    recipes = facts["recipes"]
    if "app_name" in facts:
        if not facts["args"].ignore_existing:
            create_existing_recipe_list(facts)
    else:
        raise RoboError("I wasn't able to determine the name of this app, so I "
                      "can't make any recipes.")

    preferred = [recipe for recipe in recipes if recipe["preferred"]]

    raise_if_recipes_cannot_be_generated(facts, preferred)

    # We have enough information to create a recipe set, but with assumptions.
    # TODO(Elliot): This code may not be necessary if inspections do their job.
    if "codesign_reqs" not in facts and "codesign_authorities" not in facts:
        facts["reminders"].append(
            "I can't tell whether this app is codesigned or not, so I'm "
            "going to assume it's not. You may want to verify that yourself "
            "and add the CodeSignatureVerifier processor if necessary.")
        facts["codesign_reqs"] = ""
        facts["codesign_authorities"] = []
    if "version_key" not in facts:
        facts["reminders"].append(
            "I can't tell whether to use CFBundleShortVersionString or "
            "CFBundleVersion for the version key of this app. Most apps use "
            "CFBundleShortVersionString, so that's what I'll use. You may "
            "want to verify that and modify the recipes if necessary.")
        facts["version_key"] = "CFBundleShortVersionString"

    # TODO(Elliot): Run `autopkg repo-list` once and store the resulting value for
    # future use when detecting missing required repos, rather than running
    # `autopkg repo-list` separately during each check. (For example, the
    # FileWaveImporter repo must be present to run created filewave recipes.)

    # Prepare the destination directory.
    if "developer" in facts and prefs.get("FollowOfficialJSSRecipesFormat", False) is not True:
        recipe_dest_dir = os.path.join(os.path.expanduser(prefs["RecipeCreateLocation"]), facts["developer"].replace("/", "-"))
    else:
        recipe_dest_dir = os.path.join(os.path.expanduser(prefs["RecipeCreateLocation"]), facts["app_name"].replace("/", "-"))
    facts["recipe_dest_dir"] = recipe_dest_dir
    create_dest_dirs(recipe_dest_dir)

    build_recipes(facts, preferred, prefs, recipe_dest_dir)

    # TODO (Shea): As far as I can tell, the only pref that changes is the recipe created count. Move out from here!
    # Save preferences to disk for next time.
    FoundationPlist.writePlist(prefs, PREFS_FILE)


def raise_if_recipes_cannot_be_generated(facts, preferred):
    """Raise a RoboError if recipes cannot be generated."""
    # No recipe types are preferred.
    if not preferred:
        raise RoboError("Sorry, no recipes available to generate.")

    # We don't have enough information to create a recipe set.
    if (facts["is_from_app_store"] is False and
            not any([key in facts for key in (
                    "sparkle_feed", "github_repo", "sourceforge_id",
                    "download_url")])):
        raise RoboError(
            "Sorry, I don't know how to download this app. Maybe try another "
            "angle? If you provided an app, try providing the Sparkle feed "
            "for the app instead. Or maybe the app's developers offer a "
            "direct download URL on their website.")
    if (facts["is_from_app_store"] is False and
                "download_format" not in facts):
        raise RoboError(
            "Sorry, I can't tell what format to download this app in. Maybe "
            "try another angle? If you provided an app, try providing the "
            "Sparkle feed for the app instead. Or maybe the app's developers "
            "offer a direct download URL on their website.")


def build_recipes(facts, preferred, prefs):
    """Create a recipe for each preferred type we know about."""
    recipe_dest_dir = facts["recipe_dest_dir"]
    for recipe in preferred:

        keys = recipe["keys"]
        keys["Input"]["NAME"] = facts["app_name"]

        # Set the recipe filename (spaces are OK).
        recipe["filename"] = "%s.%s.recipe" % (facts["app_name"], recipe["type"])

        # Set the recipe identifier.
        keys["Identifier"] = "%s.%s.%s" % (prefs["RecipeIdentifierPrefix"],
                                           recipe["type"],
                                           facts["app_name"].replace(" ", ""))

        # If the name of the app bundle differs from the name of the app
        # itself, we need another input variable for that.
        if "app_file" in facts:
            keys["Input"]["APP_FILENAME"] = facts["app_file"]
            facts["app_name_key"] = "%APP_FILENAME%"
        else:
            facts["app_name_key"] = "%NAME%"

        # Set keys specific to download recipes.
        generation_func = get_generation_func(facts, prefs, recipe)
        if not generation_func:
            facts["warnings"].append(
                "Oops, I think my programmer messed up. I don't yet know how "
                "to generate a %s recipe. Sorry about that." %
                recipe["type"])
        else:
            generation_func(facts, prefs, recipe)

        dest_path = os.path.join(recipe_dest_dir, recipe["filename"])
        if not os.path.exists(dest_path):
            prefs["RecipeCreateCount"] += 1
        recipe.write(dest_path)
        robo_print(dest_path, LogLevel.LOG, 4)
        facts["recipes"].append(dest_path)


def get_generation_func(facts, prefs, recipe):
    """Return the correct generation function based on type."""
    if recipe["type"] not in prefs["RecipeTypes"]:
        return None

    func_name = ["generate", recipe["type"], "recipe"]

    if recipe["type"] in ("munki", "pkg"):
        func_name.insert(1, "app_store")

    # TODO (Shea): This is a hack until I can use AbstractFactory for this.
    generation_func = globals()["_".join(func_name)]

    return generation_func


def generate_download_recipe(facts, prefs, recipe):
    """Generate a download recipe on passed recipe dict.

    Args:
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
        recipe: The recipe to operate on. This recipe will be mutated
            by this function!
    """
    keys = recipe["keys"]
    # Can't make this recipe if the app is from the App Store.
    if facts["is_from_app_store"] is True:
        facts["warnings"].append(
            "Skipping %s recipe, because this app was downloaded from the "
            "App Store." % recipe["type"])
        return

    robo_print("Generating %s recipe..." % recipe["type"])

    # Save a description that explains what this recipe does.
    keys["Description"] = ("Downloads the latest version "
                            "of %s." % facts["app_name"])

    if "sparkle_feed" in facts:
        keys["Input"]["SPARKLE_FEED_URL"] = facts["sparkle_feed"]
        if "user-agent" in facts:
            # Sparkle feed with a special user-agent.
            keys["Process"].append({
                "Processor": "SparkleUpdateInfoProvider",
                "Arguments": {
                    "appcast_request_headers": {
                        "user-agent": facts["user-agent"]
                    },
                    "appcast_url": "%SPARKLE_FEED_URL%"
                }
            })
            keys["Process"].append({
                "Processor": "URLDownloader",
                "Arguments": {
                    "filename": "%%NAME%%-%%version%%.%s" % facts["download_format"],
                    "request_headers": {
                        "user-agent": facts["user-agent"]
                    }
                }
            })
        else:
            # Sparkle feed with the default user-agent.
            keys["Process"].append({
                "Processor": "SparkleUpdateInfoProvider",
                "Arguments": {
                    "appcast_url": "%SPARKLE_FEED_URL%"
                }
            })
            keys["Process"].append({
                "Processor": "URLDownloader",
                "Arguments": {
                    "filename": "%%NAME%%-%%version%%.%s" % facts["download_format"]
                }
            })

    elif "github_repo" in facts:
        keys["Input"]["GITHUB_REPO"] = facts["github_repo"]
        recipe["keys"]["Process"].append({
            "Processor": "GitHubReleasesInfoProvider",
            "Arguments": {
                "github_repo": "%GITHUB_REPO%"
            }
        })
        keys["Process"].append({
            "Processor": "URLDownloader",
            "Arguments": {
                "filename": "%%NAME%%-%%version%%.%s" % facts["download_format"]
            }
        })
    elif "sourceforge_id" in facts:
        if "developer" in facts and prefs.get("FollowOfficialJSSRecipesFormat", False) is not True:
            create_SourceForgeURLProvider(os.path.join(os.path.expanduser(prefs["RecipeCreateLocation"]), facts["developer"]).replace("/", "-"))
        else:
            create_SourceForgeURLProvider(os.path.join(os.path.expanduser(prefs["RecipeCreateLocation"]), facts["app_name"]).replace("/", "-"))
        recipe["keys"]["Process"].append({
            "Processor": "SourceForgeURLProvider",
            "Arguments": {
                "SOURCEFORGE_FILE_PATTERN": "\\.%s" % facts["download_format"],
                "SOURCEFORGE_PROJECT_ID": facts["sourceforge_id"]
            }
        })
        keys["Process"].append({
            "Processor": "URLDownloader",
            "Arguments": {
                "filename": "%%NAME%%.%s" % facts["download_format"]
            }
        })
    elif "download_url" in facts:
        if "user-agent" in facts:
            keys["Input"]["DOWNLOAD_URL"] = facts["download_url"]
            keys["Process"].append({
                "Processor": "URLDownloader",
                "Arguments": {
                    "url": "%DOWNLOAD_URL%",
                    # TODO(Elliot): Explicit filename may not be necessary. (#35)
                    # Example: http://www.sonnysoftware.com/Bookends.dmg
                    # facts["specify_filename"] is intended to help with #35.
                    "filename": facts["download_filename"],
                    "request_headers": {
                        "user-agent": facts["user-agent"]
                    }
                }
            })
        else:
            keys["Input"]["DOWNLOAD_URL"] = facts["download_url"]
            keys["Process"].append({
                "Processor": "URLDownloader",
                "Arguments": {
                    "url": "%DOWNLOAD_URL%",
                    "filename": facts["download_filename"]
                }
            })
    keys["Process"].append({
        "Processor": "EndOfCheckPhase"
    })

    if facts.get("codesign_reqs", "") != "" or len(facts["codesign_authorities"]) > 0:
        # We encountered a signed app, and will use CodeSignatureVerifier on
        # the app.
        if facts["download_format"] in SUPPORTED_IMAGE_FORMATS:
            # We're assuming that the app is at the root level of the dmg.
            if facts.get("codesign_reqs", "") != "":
                codesigverifier_args = {
                        "input_path": "%%pathname%%/%s.app" % facts["app_name_key"],
                        "requirement": facts["codesign_reqs"]
                }
            elif len(facts["codesign_authorities"]) > 0:
                codesigverifier_args = {
                        "input_path": "%%pathname%%/%s.app" % facts["app_name_key"],
                        "expected_authorities": facts["codesign_authorities"]
                }
            keys["Process"].append({
                "Processor": "CodeSignatureVerifier",
                "Arguments": codesigverifier_args
            })
            if facts.get("sparkle_provides_version", False) is False:
                # Either the Sparkle feed doesn't provide version, or there's
                # no Sparkle feed. We must determine the version manually.
                if facts["version_key"] == "CFBundleShortVersionString":
                    keys["Process"].append({
                        "Processor": "AppDmgVersioner",
                        "Arguments": {
                            "dmg_path": "%pathname%"
                        }
                    })
                else:
                    keys["Process"].append({
                        "Processor": "Versioner",
                        "Arguments": {
                            "input_plist_path": "%%pathname%%/%s.app/Contents/Info.plist" % facts["app_name_key"],
                            "plist_version_key": facts["version_key"]
                        }
                    })
        elif facts["download_format"] in SUPPORTED_ARCHIVE_FORMATS:
            # We're assuming that the app is at the root level of the zip.
            keys["Process"].append({
                "Processor": "Unarchiver",
                "Arguments": {
                    "archive_path": "%pathname%",
                    "destination_path": "%RECIPE_CACHE_DIR%/%NAME%/Applications",
                    "purge_destination": True
                }
            })
            if facts["codesign_reqs"] != "":
                codesigverifier_args = {
                    "input_path": "%%RECIPE_CACHE_DIR%%/%%NAME%%/Applications/%s.app" % facts["app_name_key"],
                    "requirement": facts["codesign_reqs"]
                }
            elif len(facts["codesign_authorities"]) > 0:
                codesigverifier_args = {
                    "input_path": "%%RECIPE_CACHE_DIR%%/%%NAME%%/Applications/%s.app" % facts["app_name_key"],
                    "expected_authorities": facts["codesign_authorities"]
                }
            keys["Process"].append({
                "Processor": "CodeSignatureVerifier",
                "Arguments": codesigverifier_args
            })
            if facts.get("sparkle_provides_version", False) is False:
                # Either the Sparkle feed doesn't provide version, or there's
                # no Sparkle feed. We must determine the version manually.
                keys["Process"].append({
                    "Processor": "Versioner",
                    "Arguments": {
                        "input_plist_path": "%%RECIPE_CACHE_DIR%%/%%NAME%%/Applications/%s.app/Contents/Info.plist" % facts["app_name_key"],
                        "plist_version_key": facts["version_key"]
                    }
                })
        elif facts["download_format"] in SUPPORTED_INSTALL_FORMATS:
            # The download is in pkg format, and the pkg is signed.
            # TODO(Elliot): Need a few test cases to prove this works.
            keys["Process"].append({
                "Processor": "CodeSignatureVerifier",
                "Arguments": {
                    "input_path": "%pathname%",
                    "expected_authorities": facts["codesign_authorities"]
                }
            })
    # TODO(Elliot): Handle signed or unsigned pkgs wrapped in dmgs or zips.


def generate_app_store_munki_recipe(facts, prefs, recipe):
    """Generate a munki recipe on passed recipe dict.

    This function is for app-store apps.

    Args:
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
        recipe: The recipe to operate on. This recipe will be mutated
            by this function!
    """
    keys = recipe["keys"]
    robo_print("Generating %s recipe..." % recipe["type"])

    # Save a description that explains what this recipe does.
    keys["Description"] = ("Downloads the latest version of "
                            "%s from the Mac App Store and "
                            "imports it into "
                            "Munki." % facts["app_name"])
    keys["ParentRecipe"] = "com.github.nmcspadden.munki.appstore"
    keys["Input"]["PATH"] = facts["app_path"]
    recipe["filename"] = "MAS-" + recipe["filename"]

    keys["Input"]["MUNKI_REPO_SUBDIR"] = "apps/%NAME%"
    keys["Input"]["pkginfo"] = {
        "catalogs": ["testing"],
        "developer": facts.get("developer", ""),
        "display_name": facts["app_name"],
        "name": "%NAME%",
        "unattended_install": True
    }

    if "description" in facts:
        keys["Input"]["pkginfo"]["description"] = facts["description"]
    else:
        facts["reminders"].append(
            "I couldn't find a description for this app, so you'll need to "
            "manually add one to the munki recipe.")
        keys["Input"]["pkginfo"]["description"] = " "

    warn_about_appstoreapp_pyasn(facts)


def generate_munki_recipe(facts, prefs, recipe):
    """Generate a munki recipe on passed recipe dict.

    Args:
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
        prefs: The dictionary containing a key/value pair for each
            preference.
        recipe: The recipe to operate on. This recipe will be mutated
            by this function!
    """
    keys = recipe["keys"]
    robo_print("Generating %s recipe..." % recipe["type"])

    # Save a description that explains what this recipe does.
    keys["Description"] = ("Downloads the latest version of %s "
                            "and imports it into "
                            "Munki." % facts["app_name"])
    keys["ParentRecipe"] = "%s.download.%s" % (prefs["RecipeIdentifierPrefix"], facts["app_name"].replace(" ", ""))

    keys["Input"]["MUNKI_REPO_SUBDIR"] = "apps/%NAME%"
    keys["Input"]["pkginfo"] = {
        "catalogs": ["testing"],
        "developer": facts.get("developer", ""),
        "display_name": facts["app_name"],
        "name": "%NAME%",
        "unattended_install": True
    }

    if "description" in facts:
        keys["Input"]["pkginfo"]["description"] = facts["description"]
    else:
        facts["reminders"].append(
            "I couldn't find a description for this app, so you'll need to "
            "manually add one to the munki recipe.")
        keys["Input"]["pkginfo"]["description"] = " "

    # Set default variable to use for substitution.
    import_file_var = "%pathname%"

    if facts["download_format"] in SUPPORTED_IMAGE_FORMATS:
        if facts.get("codesign_reqs", "") == "" and len(facts["codesign_authorities"]) == 0:
            if facts["version_key"] == "CFBundleShortVersionString":
                keys["Process"].append({
                    "Processor": "AppDmgVersioner",
                    "Arguments": {
                        "dmg_path": "%pathname%"
                    }
                })
            else:
                keys["Process"].append({
                    "Processor": "Versioner",
                    "Arguments": {
                        "input_plist_path": "%%pathname%%/%s.app/Contents/Info.plist" % facts["app_name_key"],
                        "plist_version_key": facts["version_key"]
                    }
                })

    elif facts["download_format"] in SUPPORTED_ARCHIVE_FORMATS:
        if facts.get("codesign_reqs", "") == "" and len(facts["codesign_authorities"]) == 0:
            # If unsigned, that means the download recipe hasn't
            # unarchived the zip yet.
            keys["Process"].append({
                "Processor": "Unarchiver",
                "Arguments": {
                    "archive_path": "%pathname%",
                    "destination_path": "%RECIPE_CACHE_DIR%/%NAME%/Applications",
                    "purge_destination": True
                }
            })
        keys["Process"].append({
            "Processor": "DmgCreator",
            "Arguments": {
                "dmg_path": "%RECIPE_CACHE_DIR%/%NAME%.dmg",
                "dmg_root": "%RECIPE_CACHE_DIR%/%NAME%/Applications"
            }
        })
        import_file_var = "%dmg_path%"

    elif facts["download_format"] in SUPPORTED_INSTALL_FORMATS:
        # Blocking applications are determined automatically by Munki except
        # when the software is distributed inside a pkg. In this case, the
        # blocking applications must be set manually in the recipe.
        if len(facts["blocking_applications"]) > 0:
            keys["Input"]["pkginfo"]["blocking_applications"] = facts["blocking_applications"]

    if facts["version_key"] != "CFBundleShortVersionString":
        keys["Process"].append({
            "Processor": "MunkiPkginfoMerger",
            "Arguments": {
                "additional_pkginfo": {
                    "version": "%version%"
                }
            }
        })
        keys["Process"].append({
            "Processor": "MunkiImporter",
            "Arguments": {
                "pkg_path": import_file_var,
                "repo_subdirectory": "%MUNKI_REPO_SUBDIR%",
                "version_comparison_key": facts["version_key"]
            }
        })
    else:
        keys["Process"].append({
            "Processor": "MunkiImporter",
            "Arguments": {
                "pkg_path": import_file_var,
                "repo_subdirectory": "%MUNKI_REPO_SUBDIR%"
            }
        })

    # Extract the app's icon and save it to disk.
    if "icon_path" in facts:
        if "developer" in facts and prefs.get("FollowOfficialJSSRecipesFormat", False) is not True:
            extracted_icon = os.path.join(os.path.expanduser(prefs["RecipeCreateLocation"]), facts["developer"].replace("/", "-"), facts["app_name"] + ".png")
        else:
            extracted_icon = os.path.join(os.path.expanduser(prefs["RecipeCreateLocation"]), facts["app_name"].replace("/", "-"), facts["app_name"] + ".png")
        extract_app_icon(facts, extracted_icon)
    else:
        facts["warnings"].append(
            "I don't have enough information to create a PNG icon for this "
            "app.")


def generate_app_store_pkg_recipe(facts, prefs, recipe):
    """Generate a pkg recipe on passed recipe dict.

    This function is for app-store apps.

    Args:
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
        recipe: The recipe to operate on. This recipe will be mutated
            by this function!
    """
    keys = recipe["keys"]
    robo_print("Generating %s recipe..." % recipe["type"])

    # Save a description that explains what this recipe does.
    keys["Description"] = ("Downloads the latest version of "
                            "%s from the Mac App Store and "
                            "creates a package." % facts["app_name"])
    keys["ParentRecipe"] = "com.github.nmcspadden.pkg.appstore"
    keys["Input"]["PATH"] = facts["app_path"]
    recipe["filename"] = "MAS-" + recipe["filename"]

    warn_about_appstoreapp_pyasn(facts)


def generate_pkg_recipe(facts, prefs, recipe):
    """Generate a munki recipe on passed recipe dict.

    Args:
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
        prefs: The dictionary containing a key/value pair for each
            preference.
        recipe: The recipe to operate on. This recipe will be mutated
            by this function!
    """
    keys = recipe["keys"]
    # Can't make this recipe without a bundle identifier.
    # TODO(Elliot): Bundle id is also provided by AppDmgVersioner and some
    # Sparkle feeds. When those are present, can we proceed even though we
    # don't have bundle_id in facts? (#40)
    if "bundle_id" not in facts:
        facts["warnings"].append(
            "Skipping %s recipe, because I wasn't able to determine the "
            "bundle identifier of this app. You may want to actually download "
            "the app and try again, using the .app file itself as input."
            % recipe["type"])
        return

    robo_print("Generating %s recipe..." % recipe["type"])

    # Save a description that explains what this recipe does.
    keys["Description"] = ("Downloads the latest version of %s and "
                            "creates a package." % facts["app_name"])
    keys["ParentRecipe"] = "%s.download.%s" % (prefs["RecipeIdentifierPrefix"], facts["app_name"].replace(" ", ""))

    # Save bundle identifier.
    keys["Input"]["BUNDLE_ID"] = facts["bundle_id"]

    if facts["download_format"] in SUPPORTED_IMAGE_FORMATS:
        if facts.get("codesign_reqs", "") == "" and len(facts["codesign_authorities"]) == 0:
            if facts["version_key"] == "CFBundleShortVersionString":
                keys["Process"].append({
                    "Processor": "AppDmgVersioner",
                    "Arguments": {
                        "dmg_path": "%pathname%"
                    }
                })
            else:
                keys["Process"].append({
                    "Processor": "Versioner",
                    "Arguments": {
                        "input_plist_path": "%%pathname%%/%s.app/Contents/Info.plist" % facts["app_name_key"],
                        "plist_version_key": facts["version_key"]
                    }
                })
        keys["Process"].append({
            "Processor": "PkgRootCreator",
            "Arguments": {
                "pkgroot": "%RECIPE_CACHE_DIR%/%NAME%",
                "pkgdirs": {
                    "Applications": "0775"
                }
            }
        })
        keys["Process"].append({
            "Processor": "Copier",
            "Arguments": {
                "source_path": "%%pathname%%/%s.app" % facts["app_name_key"],
                "destination_path": "%%pkgroot%%/Applications/%s.app" % facts["app_name_key"]
            }
        })

    elif facts["download_format"] in SUPPORTED_ARCHIVE_FORMATS:
        if facts.get("codesign_reqs", "") == "" and len(facts["codesign_authorities"]) == 0:
            # If unsigned, that means the download recipe hasn't
            # unarchived the zip yet. Need to do that and version.
            keys["Process"].append({
                "Processor": "Unarchiver",
                "Arguments": {
                    "archive_path": "%pathname%",
                    "destination_path": "%RECIPE_CACHE_DIR%/%NAME%/Applications",
                    "purge_destination": True
                }
            })
            if facts.get("sparkle_provides_version", False) is False:
                # Either the Sparkle feed doesn't provide version, or there's
                # no Sparkle feed. We must determine the version manually.
                keys["Process"].append({
                    "Processor": "Versioner",
                    "Arguments": {
                        "input_plist_path": "%%RECIPE_CACHE_DIR%%/%%NAME%%/Applications/%s.app/Contents/Info.plist" % facts["app_name_key"],
                        "plist_version_key": facts["version_key"]
                    }
                })

    elif facts["download_format"] in SUPPORTED_INSTALL_FORMATS:
        facts["warnings"].append(
            "Skipping pkg recipe, since the download format is already pkg.")
        return

    keys["Process"].append({
        "Processor": "PkgCreator",
        "Arguments": {
            "pkg_request": {
                "pkgroot": "%RECIPE_CACHE_DIR%/%NAME%",
                "pkgname": "%NAME%-%version%",
                "version": "%version%",
                "id": "%BUNDLE_ID%",
                "options": "purge_ds_store",
                "chown": [{
                    "path": "Applications",
                    "user": "root",
                    "group": "admin"
                }]
            }
        }
    })


def generate_install_recipe(facts, prefs, recipe):
    """Generate an install recipe on passed recipe dict.

    Args:
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
        prefs: The dictionary containing a key/value pair for each
            preference.
        recipe: The recipe to operate on. This recipe will be mutated
            by this function!
    """
    keys = recipe["keys"]
    # Can't make this recipe if the app is from the App Store.
    if facts["is_from_app_store"] is True:
        facts["warnings"].append(
            "Skipping %s recipe, because this app was downloaded from the "
            "App Store." % recipe["type"])
        return

    robo_print("Generating %s recipe..." % recipe["type"])

    # Save a description that explains what this recipe does.
    keys["Description"] = ("Installs the latest version "
                            "of %s." % facts["app_name"])

    keys["ParentRecipe"] = "%s.download.%s" % (prefs["RecipeIdentifierPrefix"], facts["app_name"].replace(" ", ""))

    if facts["download_format"] in SUPPORTED_IMAGE_FORMATS:
        keys["Process"].append({
            "Processor": "InstallFromDMG",
            "Arguments": {
                "dmg_path": "%pathname%",
                "items_to_copy": [{
                    "source_item": "%s.app" % facts["app_name_key"],
                    "destination_path": "/Applications"
                }]
            }
        })

    elif facts["download_format"] in SUPPORTED_ARCHIVE_FORMATS:
        if facts.get("codesign_reqs", "") == "" and len(facts["codesign_authorities"]) == 0:
            keys["Process"].append({
                "Processor": "Unarchiver",
                "Arguments": {
                    "archive_path": "%pathname%",
                    "destination_path": "%RECIPE_CACHE_DIR%/%NAME%/Applications",
                    "purge_destination": True
                }
            })
        keys["Process"].append({
            "Processor": "DmgCreator",
            "Arguments": {
                "dmg_root": "%RECIPE_CACHE_DIR%/%NAME%/Applications",
                "dmg_path": "%RECIPE_CACHE_DIR%/%NAME%.dmg"
            }
        })
        keys["Process"].append({
            "Processor": "InstallFromDMG",
            "Arguments": {
                "dmg_path": "%dmg_path%",
                "items_to_copy": [{
                    "source_item": "%s.app" % facts["app_name_key"],
                    "destination_path": "/Applications"
                }]
            }
        })

    elif facts["download_format"] in SUPPORTED_INSTALL_FORMATS:
        keys["Process"].append({
            "Processor": "Installer",
            "Arguments": {
                "pkg_path": "%pathname%"
            }
        })


def generate_jss_recipe(facts, prefs, recipe):
    """Generate a JSS recipe on passed recipe dict.

    Args:
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
        prefs: The dictionary containing a key/value pair for each
            preference.
        recipe: The recipe to operate on. This recipe will be mutated
            by this function!
    """
    keys = recipe["keys"]
    # Can't make this recipe without a bundle identifier.
    if "bundle_id" not in facts:
        facts["warnings"].append(
            "Skipping %s recipe, because I wasn't able to determine the "
            "bundle identifier of this app. You may want to actually download "
            "the app and try again, using the .app file itself as input."
            % recipe["type"])
        return

    robo_print("Generating %s recipe..." % recipe["type"])

    if prefs["FollowOfficialJSSRecipesFormat"] is True:
        keys["Identifier"] = "com.github.jss-recipes.jss.%s" % facts["app_name"].replace(" ", "")

    # Save a description that explains what this recipe does.
    keys["Description"] = ("Downloads the latest version of %s "
                            "and imports it into your JSS." %
                            facts["app_name"])
    keys["ParentRecipe"] = "%s.pkg.%s" % (
        prefs["RecipeIdentifierPrefix"], facts["app_name"].replace(" ", ""))

    keys["Input"]["CATEGORY"] = "Productivity"
    facts["reminders"].append(
        "Remember to manually set the category in the jss recipe. I've set "
        "it to \"Productivity\" by default.")

    keys["Input"]["POLICY_CATEGORY"] = "Testing"
    keys["Input"]["POLICY_TEMPLATE"] = "PolicyTemplate.xml"
    keys["Input"]["SELF_SERVICE_ICON"] = "%NAME%.png"
    if not os.path.exists(os.path.join(os.path.expanduser(prefs["RecipeCreateLocation"]), "%s.png" % facts["app_name"])):
        facts["reminders"].append(
            "Please make sure %s.png is in your AutoPkg search path." %
            facts["app_name"])
    keys["Input"]["SELF_SERVICE_DESCRIPTION"] = facts.get("description", "")
    keys["Input"]["GROUP_NAME"] = "%NAME%-update-smart"

    jssimporter_arguments = {
        "prod_name": "%NAME%",
        "category": "%CATEGORY%",
        "policy_category": "%POLICY_CATEGORY%",
        "policy_template": "%POLICY_TEMPLATE%",
        "self_service_icon": "%SELF_SERVICE_ICON%",
        "self_service_description": "%SELF_SERVICE_DESCRIPTION%",
        "groups": [{
            "name": "%GROUP_NAME%",
            "smart": True,
            "template_path": "%GROUP_TEMPLATE%"
        }]
    }

    # Set variables and arguments as necessary depending on version key.
    if facts["version_key"] == "CFBundleVersion":
        keys["Input"]["GROUP_TEMPLATE"] = "CFBundleVersionSmartGroupTemplate.xml"
        jssimporter_arguments["extension_attributes"] = [{
            "ext_attribute_path": "CFBundleVersionExtensionAttribute.xml"
        }]
    else:
        keys["Input"]["GROUP_TEMPLATE"] = "SmartGroupTemplate.xml"

    # If the app's name differs from its filename, set jss_inventory_name.
    if "app_file" in facts:
        jssimporter_arguments["jss_inventory_name"] = facts["app_file"]

    # Extract the app's icon and save it to disk.
    if "icon_path" in facts:
        if "developer" in facts and prefs.get("FollowOfficialJSSRecipesFormat", False) is not True:
            extracted_icon = os.path.join(os.path.expanduser(prefs["RecipeCreateLocation"]), facts["developer"].replace("/", "-"), facts["app_name"] + ".png")
        else:
            extracted_icon = os.path.join(os.path.expanduser(prefs["RecipeCreateLocation"]), facts["app_name"].replace("/", "-"), facts["app_name"] + ".png")
        extract_app_icon(facts, extracted_icon)
    else:
        facts["warnings"].append(
            "I don't have enough information to create a PNG icon for this "
            "app.")

    # Put fully constructed JSSImporter arguments into the process list.
    keys["Process"].append({
        "Processor": "JSSImporter",
        "Arguments": jssimporter_arguments
    })


def generate_absolute_recipe(facts, prefs, recipe):
    """Generate an Absolute Manage recipe on passed recipe dict.

    Args:
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
        prefs: The dictionary containing a key/value pair for each
            preference.
        recipe: The recipe to operate on. This recipe will be mutated
            by this function!
    """
    keys = recipe["keys"]
    # Can't make this recipe without a bundle identifier.
    if "bundle_id" not in facts:
        facts["warnings"].append(
            "Skipping %s recipe, because I wasn't able to determine the "
            "bundle identifier of this app. You may want to actually download "
            "the app and try again, using the .app file itself as input."
            % recipe["type"])
        return

    robo_print("Generating %s recipe..." % recipe["type"])

    # Save a description that explains what this recipe does.
    keys["Description"] = ("Downloads the latest version of %s and "
                            "copies it into your Absolute Manage "
                            "Server." % facts["app_name"])
    keys["ParentRecipe"] = "%s.pkg.%s" % (prefs["RecipeIdentifierPrefix"], facts["app_name"])

    # Print a reminder if the required repo isn't present on disk.
    cmd = "autopkg repo-list"
    exitcode, out, err = get_exitcode_stdout_stderr(cmd)
    if not any(line.endswith("(https://github.com/tburgin/AbsoluteManageExport)") for line in out.split("\n")):
        facts["reminders"].append(
            "You'll need to add the AbsoluteManageExport repo in order to use "
            "this recipe:\nautopkg repo-add "
            "\"https://github.com/tburgin/AbsoluteManageExport\"")

    keys["Process"].append({
        "Processor": "com.github.tburgin.AbsoluteManageExport/AbsoluteManageExport",
        "SharedProcessorRepoURL": "https://github.com/tburgin/AbsoluteManageExport",
        "Arguments": {
            "dest_payload_path": "%RECIPE_CACHE_DIR%/%NAME%-%version%.amsdpackages",
            "sdpackages_ampkgprops_path": "%RECIPE_DIR%/%NAME%-Defaults.ampkgprops",
            "source_payload_path": "%pkg_path%",
            "import_abman_to_servercenter": True
        }
    })


def generate_sccm_recipe(facts, prefs, recipe):
    """Generate an SCCM recipe on passed recipe dict.

    Args:
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
        prefs: The dictionary containing a key/value pair for each
            preference.
        recipe: The recipe to operate on. This recipe will be mutated
            by this function!
    """
    keys = recipe["keys"]
    # Can't make this recipe without a bundle identifier.
    if "bundle_id" not in facts:
        facts["warnings"].append(
            "Skipping %s recipe, because I wasn't able to determine the "
            "bundle identifier of this app. You may want to actually download "
            "the app and try again, using the .app file itself as input."
            % recipe["type"])
        return

    robo_print("Generating %s recipe..." % recipe["type"])

    # Save a description that explains what this recipe does.
    keys["Description"] = ("Downloads the latest version of %s and "
                            "copies it into your SCCM "
                            "Server." % facts["app_name"])
    keys["ParentRecipe"] = "%s.pkg.%s" % (prefs["RecipeIdentifierPrefix"], facts["app_name"])

    # Print a reminder if the required repo isn't present on disk.
    cmd = "autopkg repo-list"
    exitcode, out, err = get_exitcode_stdout_stderr(cmd)
    if not any(line.endswith("(https://github.com/autopkg/cgerke-recipes)") for line in out.split("\n")):
        facts["reminders"].append(
            "You'll need to add the cgerke-recipes repo in order to use this "
            "recipe:\nautopkg repo-add "
            "\"https://github.com/autopkg/cgerke-recipes\"")

    keys["Process"].append({
        "Processor": "com.github.autopkg.cgerke-recipes.SharedProcessors/CmmacCreator",
        "SharedProcessorRepoURL": "https://github.com/autopkg/cgerke-recipes",
        "Arguments": {
            "source_file": "%pkg_path%",
            "destination_directory": "%RECIPE_CACHE_DIR%"
        }
    })


def generate_filewave_recipe(facts, prefs, recipe):
    """Generate a FileWave recipe on passed recipe dict.

    Args:
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
        prefs: The dictionary containing a key/value pair for each
            preference.
        recipe: The recipe to operate on. This recipe will be mutated
            by this function!
    """
    keys = recipe["keys"]
    # Can't make this recipe without a bundle identifier.
    if "bundle_id" not in facts:
        facts["warnings"].append(
            "Skipping %s recipe, because I wasn't able to determine the "
            "bundle identifier of this app. You may want to actually download "
            "the app and try again, using the .app file itself as input." %
            recipe["type"])
        return

    robo_print("Generating %s recipe..." % recipe["type"])

    # Save a description that explains what this recipe does.
    keys["Description"] = ("Downloads the latest version of %s, creates a "
                           "fileset, and copies it into your FileWave "
                           "Server." % facts["app_name"])
    keys["ParentRecipe"] = "%s.download.%s" % (prefs["RecipeIdentifierPrefix"], facts["app_name"])

    if facts["download_format"] in SUPPORTED_IMAGE_FORMATS and "sparkle_feed" not in facts:
        # It's a dmg download, but not from Sparkle, so we need to version it.
        keys["Process"].append({
            "Processor": "Versioner",
            "Arguments": {
                "input_plist_path": "%%pathname%%/%s.app/Contents/Info.plist" % facts["app_name_key"],
                "plist_version_key": facts["version_key"]
            }
        })
    elif facts["download_format"] in SUPPORTED_ARCHIVE_FORMATS:
        if facts.get("codesign_reqs", "") == "" and len(facts["codesign_authorities"]) == 0:
            # If unsigned, that means the download recipe hasn't
            # unarchived the zip yet.
            keys["Process"].append({
                "Processor": "Unarchiver",
                "Arguments": {
                    "archive_path": "%pathname%",
                    "destination_path": "%RECIPE_CACHE_DIR%/%NAME%/Applications",
                    "purge_destination": True
                }
            })
    elif facts["download_format"] in SUPPORTED_INSTALL_FORMATS:
        # TODO(Elliot): Fix this. (#41)
        facts["warnings"].append(
            "Sorry, I don't yet know how to create filewave recipes from pkg "
            "downloads.")

    # Print a reminder if the required repo isn't present on disk.
    cmd = "autopkg repo-list"
    exitcode, out, err = get_exitcode_stdout_stderr(cmd)
    if not any(line.endswith("(https://github.com/johncclayton/FileWaveImporter)") for line in out.split("\n")):
        facts["reminders"].append(
            "You'll need to add the FileWaveImporter repo in order to use "
            "this recipe:\nautopkg repo-add "
            "\"https://github.com/johncclayton/FileWaveImporter\"")

    keys["Process"].append({
        "Processor": "com.github.johncclayton.filewave.FWTool/FileWaveImporter",
        "Arguments": {
            "fw_app_bundle_id": facts["bundle_id"],
            "fw_app_version": "%version%",
            "fw_import_source": "%RECIPE_CACHE_DIR%/%NAME%/%NAME%.app",
            "fw_fileset_name": "%NAME% - %version%",
            "fw_fileset_group": "Testing",
            "fw_destination_root": "/Applications/%NAME%.app"
        }
    })


def generate_ds_recipe(facts, prefs, recipe):
    """Generate a DeployStudio recipe on passed recipe dict.

    Args:
        facts: A continually-updated dictionary containing all the
            information we know so far about the app associated with the
            input path.
        prefs: The dictionary containing a key/value pair for each
            preference.
        recipe: The recipe to operate on. This recipe will be mutated
            by this function!
    """
    keys = recipe["keys"]
    # Can't make this recipe without a bundle identifier.
    if "bundle_id" not in facts:
        facts["warnings"].append(
            "Skipping %s recipe, because I wasn't able to determine the "
            "bundle identifier of this app. You may want to actually download "
            "the app and try again, using the .app file itself as input."
            % recipe["type"])
        return

    robo_print("Generating %s recipe..." % recipe["type"])

    # Save a description that explains what this recipe does.
    keys["Description"] = ("Downloads the latest version of %s and "
                            "copies it to your DeployStudio "
                            "packages." % facts["app_name"])
    keys["ParentRecipe"] = "%s.pkg.%s" % (prefs["RecipeIdentifierPrefix"], facts["app_name"])
    keys["Input"]["DS_PKGS_PATH"] = prefs["DSPackagesPath"]
    keys["Input"]["DS_NAME"] = "%NAME%"
    keys["Process"].append({
        "Processor": "StopProcessingIf",
        "Arguments": {
            "predicate": "new_package_request == FALSE"
        }
    })
    keys["Process"].append({
        "Processor": "Copier",
        "Arguments": {
            "source_path": "%pkg_path%",
            "destination_path": "%DS_PKGS_PATH%/%DS_NAME%.pkg",
            "overwrite": True
        }
    })


def warn_about_appstoreapp_pyasn(facts):
    """Print warning reminding user of dependencies for AppStoreApps.

    Args:
        facts: Facts object with required key: "reminders".
    """
    facts["reminders"].append(
        "I've created at least one AppStoreApp override for you. Be sure to "
        "add the nmcspadden-recipes repo and install pyasn1, if you haven't "
        "already. (More information: "
        "https://github.com/autopkg/nmcspadden-recipes#appstoreapp-recipe)")


def main():
    """Do nothing"""
    pass


if __name__ == '__main__':
    main()
