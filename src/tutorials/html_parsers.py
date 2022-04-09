from typing import List, Tuple
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
import re
from unidecode import unidecode

from enum import Enum, auto
from src.common.registrable import Registrable

import logging

logger = logging.getLogger(__name__)

GET_CODE_BLOCK = re.compile(
    r'>>>( *)((?:[^\n])+(?:\n\.\.\. ?[^\n]+)*)+(?:\n((?:(?!>>>)[^\n]+\n?)+)\n?)?',
    flags=re.MULTILINE
)
DOUBLE_WHITESPACE = re.compile(r'\s{2,}', flags=re.MULTILINE)
REMOVE_PRINT = re.compile(r'print\(([^\n]+)\)', flags=re.DOTALL)

__all__ = [
    "TagType",
    "TutorialHTMLParser",
    "SympyParser",
    "LXMLParser",
    "PassLibParser",
    "PyNaClParser"
]


class TagType(Enum):
    PARAGRAPH = auto()
    CODE = auto()
    SECTION = auto()
    LIST = auto()
    IGNORED = auto()
    UNKNOWN = auto()


class TutorialHTMLParser(Registrable):
    NAME = None

    PARAGRAPH_CLASSES = [
        'math',
        'admonition',
        'topic',
        'versionadded',
        'versionchanged',
        'note',
        'deprecated'
    ]
    CODE_CLASSES = [
        'doctest',
        'syntax',
    ]
    IGNORED_CLASSES = [
        'toctree-wrapper',
        'graphviz'
    ]
    IGNORED_TAGS = [
        'figure'
    ]

    def get_type_of_tag(self, tag: Tag) -> TagType:
        """
        Function to get the type of the tag that determines how it will be parsed.
        Args:
            tag (Tag): the input tag

        Returns:
            The output TagType of the tag

        """
        raise NotImplementedError()

    # Overwrite these functions if the tutorial requires specific parsing.
    def parse_code(self, tag: Tag) -> str:
        return self.clean_text(tag.get_text())

    def parse_list(self, tag: Tag) -> str:
        list_items = []
        for line in tag.find_all('li'):
            list_items.append(f"* {line.text}")
        return self.clean_text('\n'.join(list_items))

    def parse_paragraph(self, tag: Tag) -> str:
        return DOUBLE_WHITESPACE.sub(
            ' ',
            self.clean_text(tag.get_text()).replace('\n', ' ')
        )

    def parse_title(self, tag: Tag) -> str:
        return self.clean_text(tag.get_text())

    def get_header_and_sections(self, soup):
        """
        Get the header and the FIRST level sections from the html
        Args:
            soup: The BS4 object.

        Returns: Two lists, the list of tags in the header, and the list of first section tags

        """
        raise NotImplementedError()

    @staticmethod
    def clean_text(text):
        return unidecode(text)

    def get_type_from_div_tag(self, tag) -> TagType:
        tag_classes = tag.attrs.get('class', [])
        if any('highlight-' in c for c in tag_classes):
            return TagType.CODE

        if any(c in tag_classes for c in self.CODE_CLASSES):
            return TagType.CODE

        elif any(c in tag_classes for c in self.PARAGRAPH_CLASSES):
            return TagType.PARAGRAPH

        elif any(c in tag_classes for c in self.IGNORED_CLASSES):
            return TagType.IGNORED
        return TagType.UNKNOWN

    #####################################################################
    # THESE ARE NOT TO BE IMPLEMENTED BY SUBCLASSES                     #
    #####################################################################

    def parse_section(
            self,
            section,
            parent_id,
            id_counter=0
    ):
        section_str_id = section.attrs['id']

        id_counter += 1
        section_id = id_counter
        section_title = None
        header_tag = [f'h{j}' for j in range(15)]
        for i, tag in enumerate(section.children):
            if isinstance(tag, NavigableString):
                continue
            if tag.name in header_tag:
                assert section_title is None
                section_title = self.parse_title(tag)
                continue

            if tag.name in self.IGNORED_TAGS:
                logger.debug(f"Skipping {tag.name} at {i}. In Ignored")
                continue

            logger.debug(f"{self.NAME}: Parsing child {i} of {section_title} for {parent_id}")
            tag_type = self.get_type_of_tag(tag)
            if tag_type == TagType.SECTION:
                logger.debug(f"Found subsection in {section_title}")
                for child_idx, child in self.parse_section(
                        section=tag,
                        parent_id=section_id,
                        id_counter=id_counter
                ):
                    id_counter = child_idx
                    yield child_idx, child
            elif not self.clean_text(tag.get_text()).strip():
                id_counter -= 1
                continue
            else:
                if section_title is None:
                    raise ValueError('section is none')
                id_counter += 1
                out = {
                    'id'            : id_counter,
                    'parent_id'     : parent_id,
                    'section_id'    : section_id,
                    'section_str_id': section_str_id,
                    'child_idx'     : i,
                    'section_title' : section_title,
                }

                if tag_type == TagType.PARAGRAPH:
                    out['text'] = self.parse_paragraph(tag)
                    out['tag'] = 'p'
                elif tag_type == TagType.CODE:
                    out['text'] = self.parse_code(tag)
                    out['tag'] = 'code'
                elif tag_type == TagType.LIST:
                    out['text'] = self.parse_list(tag)
                    out['tag'] = 'p'
                elif tag_type == TagType.IGNORED:
                    id_counter -= 1
                    continue
                else:
                    logger.error(tag.text)
                    logger.error(tag.attrs)
                    raise ValueError(f'Unknown tag type {tag.name}')

                yield id_counter, out

    def __call__(self, raw_html):
        soup = BeautifulSoup(raw_html, 'lxml')

        header, sections = self.get_header_and_sections(soup)
        parsed_sections = []
        idx_counter = 0
        for s in sections:
            sub_sections = []
            for idx, parsed in self.parse_section(
                    s, 0, idx_counter,
            ):
                sub_sections.append(parsed)
                idx_counter = idx
            parsed_sections.append(sub_sections)

        return parsed_sections


@TutorialHTMLParser.register('lxml')
class LXMLParser(TutorialHTMLParser):
    NAME = "LXML"

    def get_type_of_tag(self, tag: Tag) -> TagType:
        tag_classes = tag.attrs.get('class', [])
        if tag.name == 'div':
            if 'section' in tag_classes:
                return TagType.SECTION
            elif 'syntax' in tag_classes:
                return TagType.CODE
            elif 'note' in tag_classes:
                return TagType.PARAGRAPH
            return self.get_type_from_div_tag(tag)
        elif tag.name == 'pre':
            if 'literal-block' in tag_classes:
                return TagType.CODE
        elif tag.name == 'p':
            return TagType.PARAGRAPH
        elif tag.name in ['ul', 'ol', 'blockquote']:
            return TagType.LIST
        elif tag.name in ['table', 'img', 'dl']:
            return TagType.IGNORED

        return TagType.UNKNOWN

    def get_header_and_sections(self, soup) -> Tuple[List[Tag], List[Tag]]:

        body = soup.find(
            'div',
            {'class': 'document'}
        )

        return [], body.find_all('div', {'class': 'section'}, recursive=False)


@TutorialHTMLParser.register('sympy')
class SympyParser(TutorialHTMLParser):
    NAME = "sympy"

    def parse_code(self, tag: Tag) -> str:
        code_block = tag.find('pre')
        assert code_block is not None
        return self.clean_text(code_block.get_text()).lstrip()

    def parse_title(self, tag: Tag) -> str:
        header_link = tag.find('a')
        if header_link is not None:
            header_link.extract()
        return self.clean_text(tag.get_text())

    def get_type_of_tag(self, tag: Tag) -> TagType:
        if tag.name == 'section':
            return TagType.SECTION
        elif tag.name == 'div':
            return self.get_type_from_div_tag(tag)
        elif tag.name == 'p':
            return TagType.PARAGRAPH
        elif tag.name in ['ul', 'ol', 'blockquote']:
            return TagType.LIST
        elif tag.name in ['table', 'img', 'dl', 'aside']:
            return TagType.IGNORED

        return TagType.UNKNOWN

    def get_header_and_sections(self, soup) -> Tuple[List[Tag], List[Tag]]:

        body = soup.find(
            'div',
            {'class': 'body'}
        )

        return [], body.find_all('section', recursive=False)


@TutorialHTMLParser.register('passlib')
class PassLibParser(TutorialHTMLParser):
    NAME = "passlib"

    def parse_code(self, tag: Tag) -> str:
        code_block = tag.find('pre')
        assert code_block is not None
        return self.clean_text(code_block.get_text()).lstrip()

    def parse_title(self, tag: Tag) -> str:
        header_link = tag.find('a')
        if header_link is not None:
            header_link.extract()
        return self.clean_text(tag.get_text())

    def get_type_of_tag(self, tag: Tag) -> TagType:
        tag_classes = tag.attrs.get('class', [])
        if tag.name == 'div':
            if 'section' in tag_classes:
                return TagType.SECTION
            return self.get_type_from_div_tag(tag)
        elif tag.name == 'p':
            return TagType.PARAGRAPH
        elif tag.name in ['ul', 'ol', 'blockquote']:
            return TagType.LIST
        elif tag.name in ['table', 'img', 'dl', 'aside']:
            return TagType.IGNORED

        return TagType.UNKNOWN

    def get_header_and_sections(self, soup) -> Tuple[List[Tag], List[Tag]]:

        body = soup.find(
            'div',
            {'class': 'body'}
        )

        return [], body.find_all('div', {'class': 'section'}, recursive=False)


@TutorialHTMLParser.register('pynacl')
class PyNaClParser(TutorialHTMLParser):
    NAME = "pynacl"

    def parse_code(self, tag: Tag) -> str:
        code_block = tag.find('pre')
        assert code_block is not None
        return self.clean_text(code_block.get_text()).lstrip()

    def parse_title(self, tag: Tag) -> str:
        header_link = tag.find('a')
        if header_link is not None:
            header_link.extract()
        return self.clean_text(tag.get_text())

    def get_type_of_tag(self, tag: Tag) -> TagType:
        if tag.name == 'section':
            return TagType.SECTION
        elif tag.name == 'div':
            return self.get_type_from_div_tag(tag)
        elif tag.name == 'p':
            return TagType.PARAGRAPH
        elif tag.name in ['ul', 'ol', 'blockquote']:
            return TagType.LIST
        elif tag.name in ['table', 'img', 'dl', 'aside']:
            return TagType.IGNORED

        return TagType.UNKNOWN

    def get_header_and_sections(self, soup) -> Tuple[List[Tag], List[Tag]]:

        body = soup.find(
            'div',
            {'itemprop': 'articleBody'}
        )

        return [], body.find_all('section', recursive=False)
