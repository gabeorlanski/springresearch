"""
Code for handling
"""
import logging
from copy import deepcopy
from datetime import datetime
from typing import Callable, List, Dict, Iterator
from bs4 import BeautifulSoup
from bs4.element import Tag
import re
from unidecode import unidecode

from jinja2 import BaseLoader, Environment, StrictUndefined
from src.common import PROJECT_ROOT

logger = logging.getLogger(__name__)
logging.getLogger("transformers.tokenization_utils").setLevel(logging.ERROR)
JINJA_ENV = Environment(loader=BaseLoader)  # type:ignore

# Allow the python function zip()
JINJA_ENV.globals.update(zip=zip)
JINJA_ENV.undefined = StrictUndefined


class StackOverflowProcessor:
    def __init__(
            self,
            prompt_file: str,
            answer_sorting: str = 'accepted',
            repeat_prompt_each_answer: bool = False,
            answers_per_sample: int = -1,
            top_answer_cutoff: int = 12,
            good_answer_cutoff: int = 3,
            bad_answer_cutoff: int = -1,
            remove_modality: str = "NONE",
            no_answer_str: str = "There is not an answer",
            allow_no_answer: bool = False,
            comment_type_for_question: str = 'NONE',
            repeat_body_for_each_answer: bool = False,
            wrap_answer_character: str = None,
            include_date: bool = True,
            include_question_score: bool = True,
            include_tags: bool = True,
            include_quality: bool = True,
            date_format_str: str = "%Y"
    ):
        self.answer_sorting = answer_sorting.lower()
        if self.answer_sorting not in ['ascending', 'descending', 'accepted']:
            raise ValueError(f"Unknown answer sorting method: {self.answer_sorting}")

        self.prompt = JINJA_ENV.from_string(
            PROJECT_ROOT.joinpath(prompt_file).read_text()
        )  # type:ignore
        self.good_answer_cutoff = good_answer_cutoff
        self.bad_answer_cutoff = bad_answer_cutoff
        self.top_answer_cutoff = top_answer_cutoff
        self.answers_per_sample = answers_per_sample
        self.repeat_prompt_each_answer = repeat_prompt_each_answer
        self.comment_type_for_question = comment_type_for_question
        self.repeat_body_for_each_answer = repeat_body_for_each_answer
        self.allow_no_answer = allow_no_answer
        self.no_answer_str = no_answer_str
        self.include_question_score = include_question_score
        self.include_tags = include_tags
        self.include_date = include_date
        self.date_format_str = date_format_str
        self.include_quality = include_quality
        self.wrap_answer_character = wrap_answer_character
        if wrap_answer_character:
            if wrap_answer_character.upper() in ['BLOCK', 'LINE']:
                self.wrap_answer_character = wrap_answer_character.upper()
            elif wrap_answer_character.upper() == 'NONE':
                self.wrap_answer_character = None
            else:
                raise ValueError(f"Unknown answer wrap {wrap_answer_character=}, disabling")
        if remove_modality is None:
            self.remove_modality = "NONE"
        else:
            self.remove_modality = remove_modality.upper()
            if self.remove_modality not in ['CODE', 'NL', 'NONE']:
                self.remove_modality = 'NONE'

    def clean_html_body(self, body_str, force_keep_all=False) -> List[Tag]:
        soup = BeautifulSoup(body_str, 'lxml')
        body = soup.find('body')

        out = []
        for i, tag in enumerate(body.find_all(recursive=False)):

            # Check if in code block
            if tag.name == 'pre':
                if self.remove_modality == "CODE" and not force_keep_all:
                    continue
                code = tag.find('code', recursive=False)
                if code is None:
                    code = tag
                new_tag = soup.new_tag('code')
                new_tag.string = code.text.strip()
                out.append(new_tag)
            else:
                if self.remove_modality == 'NL' and not force_keep_all:
                    continue
                nl_text = tag.text.strip().replace('"""', '\"\"\"')
                if out and out[-1].name == 'p':
                    out[-1].string = f"{out[-1].string}\n{nl_text}"
                else:
                    new_tag = soup.new_tag('p')
                    new_tag.string = nl_text
                    out.append(new_tag)

        return out

    def turn_body_into_str(self, body_tags: List[Tag]) -> str:
        out = []
        for t in body_tags:
            if not t.string:
                continue
            if t.name == 'p':
                out.append(self.wrap_nl(t.text.strip()))
            else:
                out.append(t.text.strip())
        return unidecode('\n'.join(o for o in out if o.strip()))

    def wrap_nl(self, nl_str):
        if not nl_str:
            return ''

        wrap_char = self.wrap_answer_character

        if wrap_char:
            if wrap_char == "BLOCK":
                return f'"""\n{nl_str.strip()}\n"""'
            else:
                return f"# {nl_str.strip()}"
        else:
            return nl_str.strip()

    def process_question(
            self,
            title: str,
            body: List[Tag],
            score,
            views,
            date,
            tags
    ):

        question_date = None
        if self.include_date:
            question_date = datetime.fromisoformat(date).strftime(self.date_format_str)

        return {
            "title"         : title,
            "question_score": score if self.include_question_score else None,
            "tags"          : ','.join(tags) if self.include_tags else None,
            'question'      : unidecode('\n'.join(t.text.strip() for t in body if t.text.strip())),
            'question_date' : question_date
        }

    def process_answer(self, answer: List[Tag], score, is_accepted):

        if not answer:
            return self.no_answer_str

        quality_str = ""
        if self.good_answer_cutoff is not None and self.bad_answer_cutoff is not None:
            if is_accepted:
                quality_str = 'ACCEPTED'
            elif score >= self.top_answer_cutoff:
                quality_str = 'GREAT'
            elif score >= self.good_answer_cutoff:
                quality_str = "GOOD"
            elif score <= self.bad_answer_cutoff:
                quality_str = "BAD"
            else:
                quality_str = "OK"

        return quality_str, self.turn_body_into_str(answer)

    def apply_prompt(self, prompt_kwargs, is_first_answer, answer_quality=None):
        copy_prompt_kwargs = deepcopy(prompt_kwargs)
        if not is_first_answer and not self.repeat_body_for_each_answer:
            copy_prompt_kwargs['question'] = None
        if answer_quality:
            copy_prompt_kwargs['quality'] = answer_quality

        if not self.include_quality:
            copy_prompt_kwargs['quality'] = None

        return self.prompt.render(**copy_prompt_kwargs).strip()

    def __call__(self, sample: Dict) -> List[Dict]:
        """
        Get the text string from the sample.
        """
        # Set to -1 if there is no accepted answer because it is impossible.
        accepted_answer_id = sample['accepted_answer'] or "-1"

        sample['body'] = self.clean_html_body(sample['body'],
                                              force_keep_all=True)

        for k in sample['answers'].keys():
            sample['answers'][k]['body'] = self.clean_html_body(sample['answers'][k]['body'])

        # Do a list comprehension to eliminate the accepted answer
        accepted_answer = None
        answers = []
        for d in sample['answers'].values():
            if d['id'] == accepted_answer_id and self.answer_sorting == "accepted":
                accepted_answer = d
            else:
                answers.append(d)

        # Sort the answer keys
        answers = list(sorted(
            answers,
            key=lambda ans: ans['score'],
            reverse=self.answer_sorting != 'ascending'
        ))

        # If there is an accepted answer and we are sorting by accepted, put the
        # accepted at the front of the list.
        if accepted_answer is not None and self.answer_sorting == "accepted":
            answers = [accepted_answer, *answers]

        # Create the kwargs for the prompt.
        prompt_kwargs = self.process_question(
            title=sample['title'],
            body=deepcopy(sample['body']),
            score=sample['score'],
            views=sample['views'],
            tags=sample['tags'],
            date=sample['date']
        )
        prompt_kwargs['quality'] = 'NONE' if self.repeat_prompt_each_answer else 'ACCEPTED'
        prompt_kwargs['comment_type'] = self.comment_type_for_question

        if self.answers_per_sample == -1:
            answers_keep = len(answers)
        else:
            answers_keep = self.answers_per_sample

        # Add the quality information to the answer.
        if not answers:
            if self.allow_no_answer:
                return [
                    {
                        'input' : self.apply_prompt(prompt_kwargs, True),
                        'labels': self.no_answer_str
                    }
                ]
            return []

        answers_processed = []
        for i, answer in enumerate(answers):
            if not answer['body'] and not self.allow_no_answer:
                continue
            if i >= answers_keep:
                break
            answers_processed.append(
                self.process_answer(
                    answer['body'], answer['score'],
                    answer['id'] == accepted_answer_id
                )
            )

        if not answers_processed:
            if not self.allow_no_answer:
                return []
            return [
                {
                    'input' : self.apply_prompt(prompt_kwargs, True),
                    'labels': self.no_answer_str
                }
            ]
        if not self.repeat_prompt_each_answer:
            return [
                {
                    'input' : self.apply_prompt(prompt_kwargs, True),
                    'labels': '\n'.join(d[1] for d in answers_processed)
                }
            ]

        return [
            {
                'input' : self.apply_prompt(prompt_kwargs, i == 0, d[0]),
                'labels': d[1]
            }
            for i, d in enumerate(answers_processed)
        ]
