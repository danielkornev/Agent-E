import json
import os
import tempfile
import traceback
from string import Template
import autogen  # type: ignore
import openai
#from autogen import Cache
from dotenv import load_dotenv
from ae.core.agents.browser_nav_agent import BrowserNavAgent
from ae.core.agents.high_level_planner_agent import PlannerAgent  
from ae.core.prompts import LLM_PROMPTS
from ae.utils.logger import logger


class AutogenWrapper:
    """
    A wrapper class for interacting with the Autogen library.

    Args:
        max_chat_round (int): The maximum number of chat rounds.

    Attributes:
        number_of_rounds (int): The maximum number of chat rounds.
        agents_map (dict): A dictionary of the agents that are instantiated in this autogen instance.

    """

    def __init__(self, max_chat_round: int = 50):
        self.number_of_rounds = max_chat_round
        self.agents_map: dict[str, autogen.UserProxyAgent | autogen.AssistantAgent | autogen.ConversableAgent ] | None = None
        self.config_list: list[dict[str, str]] | None = None

    @classmethod
    async def create(cls, agents_needed: list[str] | None = None, max_chat_round: int = 50):
        """
        Create an instance of AutogenWrapper.

        Args:
            agents_needed (list[str], optional): The list of agents needed. If None, then ["user_proxy", "browser_nav_agent"] will be used.
            max_chat_round (int, optional): The maximum number of chat rounds. Defaults to 50.

        Returns:
            AutogenWrapper: An instance of AutogenWrapper.

        """
        if agents_needed is None:
            agents_needed = ["user_proxy", "browser_nav_executor", "planner_agent", "browser_nav_agent"]
        # Create an instance of cls
        self = cls(max_chat_round)
        load_dotenv()
        os.environ["AUTOGEN_USE_DOCKER"] = "False"
        env_var = [{'model': 'gpt-4-turbo-preview', 'api_key': os.environ['OPENAI_API_KEY']}]
        with tempfile.NamedTemporaryFile(delete=False, mode='w') as temp:
            json.dump(env_var, temp)
            temp_file_path = temp.name

        self.config_list = autogen.config_list_from_json(env_or_file=temp_file_path, filter_dict={"model": {"gpt-4-turbo-preview"}}) # type: ignore
        self.agents_map = await self.__initialize_agents(agents_needed)
        
        def nested_chat_message(
            recipient: autogen.AssistantAgent, 
            messages: list[dict[str, str]], 
            sender: autogen.UserProxyAgent, 
            config: dict[str, str]
        ) -> str: # type: ignore
            print("************ inside nested_chat_message ************")
            return f"{messages[0]['content']}"

        self.agents_map["user_proxy"].register_nested_chats( # type: ignore
            [
                {
                    "sender": self.agents_map["browser_nav_executor"],
                    "recipient": self.agents_map["browser_nav_agent"],
                    "message": nested_chat_message,
                    "max_turns": 2,
                    "summary_method": "last_msg",
                }
                    
            ],
            trigger=self.agents_map["planner_agent"],
        )

        return self


    async def __initialize_agents(self, agents_needed: list[str]):
        """
        Instantiate all agents with their appropriate prompts/skills.

        Args:
            agents_needed (list[str]): The list of agents needed, this list must have user_proxy in it or an error will be generated.

        Returns:
            dict: A dictionary of agent instances.

        """
        if "user_proxy" not in agents_needed:
            raise ValueError("user_proxy agent is required in the list of needed agents.")

        agents_map: dict[str, autogen.AssistantAgent | autogen.UserProxyAgent  | autogen.ConversableAgent]= {}

        user_proxy_agent = await self.__create_user_proxy_agent()
        user_proxy_agent.reset()
        agents_map["user_proxy"] = user_proxy_agent
        agents_needed.remove("user_proxy")
        
        browser_nav_executor = self.__create_browser_nav_executor_agent()
        agents_map["browser_nav_executor"] = browser_nav_executor
        browser_nav_executor.reset()
        agents_needed.remove("browser_nav_executor")
        
        for agent_needed in agents_needed:
            if agent_needed == "browser_nav_agent":
                browser_nav_agent: autogen.AssistantAgent = self.__create_browser_nav_agent(browser_nav_executor)
                browser_nav_agent.reset()
                agents_map["browser_nav_agent"] = browser_nav_agent
            elif agent_needed == "planner_agent":
                planner_agent = self.__create_planner_agent()
                planner_agent.reset()
                agents_map["planner_agent"] = planner_agent
            else:
                raise ValueError(f"Unknown agent type: {agent_needed}")
        return agents_map


    async def __create_user_proxy_agent(self):
        """
        Create a UserProxyAgent instance.

        Returns:
            autogen.UserProxyAgent: An instance of UserProxyAgent.

        """
        user_proxy_agent = autogen.UserProxyAgent(
            name="user_proxy",
            system_message=LLM_PROMPTS["USER_AGENT_PROMPT"],
            is_termination_msg=lambda x: x.get("content", "") and x.get("content", "").rstrip().upper().endswith("##TERMINATE##"), # type: ignore
            human_input_mode="NEVER",
            max_consecutive_auto_reply=self.number_of_rounds,
        )
        print(">>> user_proxy_agent:", user_proxy_agent)
        return user_proxy_agent

    def __create_browser_nav_executor_agent(self):
        """
        Create a UserProxyAgent instance for executing browser control.

        Returns:
            autogen.UserProxyAgent: An instance of UserProxyAgent.

        """
        browser_nav_executor_agent = autogen.UserProxyAgent(
            name="browser_nav_executor",
            system_message=LLM_PROMPTS["BROWSER_NAV_EXECUTOR_PROMPT"],
            is_termination_msg=lambda x: x.get("content", "") and x.get("content", "").rstrip().upper().endswith("##TERMINATE SUBTASK##"), # type: ignore
            human_input_mode="NEVER",
            max_consecutive_auto_reply=self.number_of_rounds,
            code_execution_config={
                                "last_n_messages": 1,
                                "work_dir": "tasks",
                                "use_docker": False,
                                },
        )
        print(">>> Created browser_nav_executor_agent:", browser_nav_executor_agent)
        return browser_nav_executor_agent

    def __create_browser_nav_agent(self, user_proxy_agent: autogen.UserProxyAgent) -> autogen.AssistantAgent:
        """
        Create a BrowserNavAgent instance.

        Args:
            user_proxy_agent (autogen.UserProxyAgent): The instance of UserProxyAgent that was created.

        Returns:
            autogen.AssistantAgent: An instance of BrowserNavAgent.

        """
        browser_nav_agent = BrowserNavAgent(self.config_list, user_proxy_agent) # type: ignore
        #print(">>> browser agent tools:", json.dumps(browser_nav_agent.agent.llm_config.get("tools"), indent=2))
        print(">>> browser_nav_agent:", browser_nav_agent.agent)
        return browser_nav_agent.agent

    def __create_planner_agent(self):
        """
        Create a Planner Agent instance. This is mainly used for exploration at this point

        Returns:
            autogen.AssistantAgent: An instance of PlannerAgent.

        """
        planner_agent = PlannerAgent(self.config_list) # type: ignore
        print(">>> planner_agent:", planner_agent.agent)
        return planner_agent.agent

    async def process_command(self, command: str, current_url: str | None = None):
        """
        Process a command by sending it to one or more agents.

        Args:
            command (str): The command to be processed.
            current_url (str, optional): The current URL of the browser. Defaults to None.

        """
        current_url_prompt_segment = ""
        if current_url:
            current_url_prompt_segment = f"Current URL: {current_url}"

        prompt = Template(LLM_PROMPTS["COMMAND_EXECUTION_PROMPT"]).substitute(command=command, current_url_prompt_segment=current_url_prompt_segment)
        logger.info(f"Prompt for command: {prompt}")
        #with Cache.disk() as cache:
        try:
            if self.agents_map is None:
                raise ValueError("Agents map is not initialized.")
            print(f">>> agents_map: {self.agents_map}") 
            await self.agents_map["user_proxy"].a_initiate_chat( # type: ignore
                self.agents_map["planner_agent"], # self.manager # type: ignore
                #clear_history=True,
                message=prompt,
                silent=False,
                cache=None,
            )
        except openai.BadRequestError as bre:
            logger.error(f"Unable to process command: \"{command}\". {bre}")
            traceback.print_exc()


    
