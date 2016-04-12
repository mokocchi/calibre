#!/usr/bin/env python2
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

from __future__ import (unicode_literals, division, absolute_import,
                        print_function)
from collections import defaultdict, namedtuple
from functools import partial
from itertools import count
from operator import itemgetter
import re

from cssutils.css import CSSStyleSheet, CSSRule, Property

from css_selectors import Select, INAPPROPRIATE_PSEUDO_CLASSES, SelectorError
from calibre import as_unicode
from calibre.ebooks.css_transform_rules import all_properties
from calibre.ebooks.oeb.base import OEB_STYLES, XHTML
from calibre.ebooks.oeb.normalize_css import normalizers, DEFAULTS
from calibre.ebooks.oeb.stylizer import media_ok, INHERITED

_html_css_stylesheet = None

def html_css_stylesheet(container):
    global _html_css_stylesheet
    if _html_css_stylesheet is None:
        data = P('templates/html.css', data=True).decode('utf-8')
        _html_css_stylesheet = container.parse_css(data, 'user-agent.css')
    return _html_css_stylesheet

def media_allowed(media):
    if not media or not media.mediaText:
        return True
    return media_ok(media.mediaText)

def iterrules(container, sheet_name, rules=None, media_rule_ok=media_allowed, rule_index_counter=None, rule_type=None, importing=None):
    ''' Iterate over all style rules in the specified sheet. Import and Media rules are
    automatically resolved. Yields (rule, sheet_name, rule_number).

    :param rules: List of CSSRules or a CSSStyleSheet instance or None in which case it is read from container using sheet_name
    :param sheet_name: The name of the sheet in the container (in case of inline style sheets, the name of the html file)
    :param media_rule_ok: A function to test if a @media rule is allowed
    :param rule_index_counter: A counter object, rule numbers will be calculated by incrementing the counter.
    :param rule_type: Only yield rules of this type, where type is a string type name, see cssutils.css.CSSRule for the names (by default all rules are yielded)
    :return: (CSSRule object, the name of the sheet from which it comes, rule index - a monotonically increasing number)
    '''

    rule_index_counter = rule_index_counter or count()
    if importing is None:
        importing = set()
    importing.add(sheet_name)
    riter = partial(iterrules, container, rule_index_counter=rule_index_counter, media_rule_ok=media_rule_ok, rule_type=rule_type, importing=importing)
    if rules is None:
        rules = container.parsed(sheet_name)
    if rule_type is not None:
        rule_type = getattr(CSSRule, rule_type)

    for rule in rules:
        if rule.type == CSSRule.IMPORT_RULE:
            if media_rule_ok(rule.media):
                name = container.href_to_name(rule.href, sheet_name)
                if container.has_name(name):
                    if name in importing:
                        container.log.error('Recursive import of {} from {}, ignoring'.format(name, sheet_name))
                    else:
                        csheet = container.parsed(name)
                        if isinstance(csheet, CSSStyleSheet):
                            for cr in riter(name, rules=csheet):
                                yield cr
        elif rule.type == CSSRule.MEDIA_RULE:
            if media_rule_ok(rule.media):
                for cr in riter(sheet_name, rules=rule.cssRules):
                    yield cr

        elif rule_type is None or rule.type == rule_type:
            num = next(rule_index_counter)
            yield rule, sheet_name, num

    importing.discard(sheet_name)

StyleDeclaration = namedtuple('StyleDeclaration', 'index declaration pseudo_element')
Specificity = namedtuple('Specificity', 'is_style num_id num_class num_elem rule_index')

def specificity(rule_index, selector, is_style=0):
    s = selector.specificity
    return Specificity(is_style, s[1], s[2], s[3], rule_index)

def iterdeclaration(decl):
    for p in all_properties(decl):
        n = normalizers.get(p.name)
        if n is None:
            yield p
        else:
            for k, v in n(p.name, p.propertyValue).iteritems():
                yield Property(k, v, p.literalpriority)

class Values(tuple):

    ''' A tuple of `cssutils.css.Value ` (and its subclasses) objects. Also has a
    `sheet_name` attribute that is the canonical name relative to which URLs
    for this property should be resolved. '''

    def __new__(typ, pv, sheet_name=None, priority=''):
        ans = tuple.__new__(typ, pv)
        ans.sheet_name = sheet_name
        ans.is_important = priority == 'important'
        return ans

    @property
    def cssText(self):
        if len(self) == 1:
            return self[0].cssText
        return tuple(x.cssText for x in self)

def normalize_style_declaration(decl, sheet_name):
    ans = {}
    for prop in iterdeclaration(decl):
        ans[prop.name] = Values(prop.propertyValue, sheet_name, prop.priority)
    return ans

def resolve_declarations(decls):
    property_names = set()
    for d in decls:
        property_names |= set(d.declaration)
    ans = {}
    for name in property_names:
        first_val = None
        for decl in decls:
            x = decl.declaration.get(name)
            if x is not None:
                if x.is_important:
                    first_val = x
                    break
                if first_val is None:
                    first_val = x
        ans[name] = first_val
    return ans

def resolve_styles(container, name, select=None):
    root = container.parsed(name)
    select = select or Select(root, ignore_inappropriate_pseudo_classes=True)
    style_map = defaultdict(list)
    pseudo_style_map = defaultdict(list)
    rule_index_counter = count()
    pseudo_pat = re.compile(ur':{1,2}(%s)' % ('|'.join(INAPPROPRIATE_PSEUDO_CLASSES)), re.I)

    def process_sheet(sheet, sheet_name):
        for rule, sheet_name, rule_index in iterrules(container, sheet_name, rules=sheet, rule_index_counter=rule_index_counter, rule_type='STYLE_RULE'):
            for selector in rule.selectorList:
                text = selector.selectorText
                try:
                    matches = tuple(select(text))
                except SelectorError as err:
                    container.log.error('Ignoring CSS rule with invalid selector: %r (%s)' % (text, as_unicode(err)))
                    continue
                m = pseudo_pat.search(text)
                style = normalize_style_declaration(rule.style, sheet_name)
                if m is None:
                    for elem in matches:
                        style_map[elem].append(StyleDeclaration(specificity(rule_index, selector), style, None))
                else:
                    for elem in matches:
                        pseudo_style_map[elem].append(StyleDeclaration(specificity(rule_index, selector), style, m.group(1)))

    process_sheet(html_css_stylesheet(container), 'user-agent.css')

    for elem in root.iterdescendants(XHTML('style'), XHTML('link')):
        if elem.tag.lower().endswith('style'):
            if not elem.text:
                continue
            sheet = container.parse_css(elem.text)
            sheet_name = name
        else:
            if (elem.get('type') or 'text/css').lower() not in OEB_STYLES or \
                    (elem.get('rel') or 'stylesheet').lower() != 'stylesheet' or \
                    not media_ok(elem.get('media')):
                continue
            href = elem.get('href')
            if not href:
                continue
            sheet_name = container.href_to_name(href, name)
            if not container.has_name(sheet_name):
                continue
            sheet = container.parsed(sheet_name)
            if not isinstance(sheet, CSSStyleSheet):
                continue
        process_sheet(sheet, sheet_name)

    for elem in root.xpath('//*[@style]'):
        text = elem.get('style')
        if text:
            style = container.parse_css(text, is_declaration=True)
            style_map[elem].append(StyleDeclaration(Specificity(1, 0, 0, 0, 0), normalize_style_declaration(style, name), None))

    for l in (style_map, pseudo_style_map):
        for x in l.itervalues():
            x.sort(key=itemgetter(0), reverse=True)

    style_map = {elem:resolve_declarations(x) for elem, x in style_map.iteritems()}
    pseudo_style_map = {elem:resolve_declarations(x) for elem, x in pseudo_style_map.iteritems()}

    return style_map, pseudo_style_map, select

_defvals = None

def defvals():
    global _defvals
    if _defvals is None:
        u = type('')
        _defvals = {k:Values(Property(k, u(val)).propertyValue) for k, val in DEFAULTS.iteritems()}
    return _defvals

def resolve_property(elem, name, style_map):
    ''' Given a `style_map` previously generated by :func:`resolve_styles()` and
    a property `name`, returns the effective value of that property for the
    specified element. Handles inheritance and CSS cascading rules. Returns
    an instance of :class:`Values`. If the property was never set and
    is not a known property, then it will return None. '''

    inheritable = name in INHERITED
    q = elem
    while q is not None:
        s = style_map.get(q)
        if s is not None:
            val = s.get(name)
            if val is not None:
                return val
        q = q.getparent() if inheritable else None
    return defvals().get(name)